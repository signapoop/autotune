from __future__ import division
import os
import torch
import torch.nn as nn
from torch.autograd import Variable

from ..core.problem_def import Problem
from ..core.params import *
from ..util.progress_bar import progress_bar
from data.cifar_data_loader import get_train_val_set, get_test_set
from ml_models.cudaconvnet2 import CudaConvNet2


class CifarProblemNew(Problem):

    def __init__(self, data_dir, output_dir):
        self.data_dir = data_dir
        self.output_dir = output_dir
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        self.initialise_data()
        self.domain = self.initialise_domain()

        self.use_cuda = torch.cuda.is_available()
        print("Using GPUs? : {}".format(self.use_cuda))

        self.hps = None

    def initialise_data(self):
        # 40k train, 10k val, 10k test
        print('==> Preparing data..')
        train_data, val_data, train_sampler, val_sampler = get_train_val_set(data_dir=self.data_dir,
                                                                             augment=True,
                                                                             random_seed=0,
                                                                             valid_size=0.2)
        test_data = get_test_set(data_dir=self.data_dir)

        self.val_loader = torch.utils.data.DataLoader(val_data, batch_size=100, sampler=val_sampler,
                                                      num_workers=2, pin_memory=False)
        self.test_loader = torch.utils.data.DataLoader(test_data, batch_size=100, shuffle=True,
                                                       num_workers=2, pin_memory=False)
        self.train_data = train_data
        self.train_sampler = train_sampler

    def generate_arms(self, n, hps=None):
        arms = super(CifarProblemNew, self).generate_arms(n, hps)
        os.chdir(self.output_dir)

        def construct_model(arm):
            arm['filename'] = arm['dir'] + "/model.pth"
            # Construct model and optimizer
            base_lr = arm['learning_rate']
            n_units_1 = arm['n_units_1']
            n_units_2 = arm['n_units_2']
            n_units_3 = arm['n_units_3']
            weight_decay = arm['weight_decay']
            momentum = arm['momentum']

            model = CudaConvNet2(n_units_1, n_units_2, n_units_3)
            if self.use_cuda:
                model.cuda()
                model = torch.nn.DataParallel(model, device_ids=range(torch.cuda.device_count()))
                torch.backends.cudnn.benchmark = True

            optimizer = torch.optim.SGD(model.parameters(), lr=base_lr, momentum=momentum, weight_decay=weight_decay)

            torch.save({
                'epoch': 0,
                'model': model,
                'optimizer': optimizer,
                'val_error': 1,
                'test_error': 1,
            }, arm['filename'])

            return arm['filename']

        subdirs = next(os.walk('.'))[1]
        if len(subdirs) == 0:
            start_count = 0
        else:
            start_count = len(subdirs)

        for i in range(n):
            dirname = "arm" + str(start_count+i)
            if not os.path.exists(dirname):
                os.makedirs(dirname)
            arms[i]['dir'] = self.output_dir + dirname
            construct_model(arms[i])
        return arms

    def eval_arm(self, arm, n_resources):
        print("\nLoading arm with parameters.....")
        print(arm)

        checkpoint = torch.load(arm['filename'])
        start_epoch = checkpoint['epoch']
        model = checkpoint['model']
        optimizer = checkpoint['optimizer']

        # Rest of the tunable hyperparameters
        base_lr = arm['learning_rate']
        batch_size = arm['batch_size']
        lr_step = arm['lr_step']
        gamma = arm['gamma']

        # Initialise train_loader based on batch size
        train_loader = torch.utils.data.DataLoader(self.train_data, batch_size=batch_size,
                                                   sampler=self.train_sampler,
                                                   num_workers=2, pin_memory=False)

        # Compute derived hyperparameters
        n_batches = int(n_resources * 10000 / batch_size)  # each unit of resource = 10,000 examples
        batches_per_epoch = len(train_loader)
        max_epochs = int(n_batches / batches_per_epoch) + 1

        if lr_step > max_epochs or lr_step == 0:
            step_size = max_epochs
        else:
            step_size = int(max_epochs / lr_step)

        criterion = nn.CrossEntropyLoss()

        def adjust_learning_rate(optimizer, epoch):
            """Sets the learning rate to the initial LR decayed by gamma every step_size epochs"""
            lr = base_lr * (gamma ** (epoch // step_size))
            for param_group in optimizer.param_groups:
                param_group['lr'] = lr

        # Training
        def train(loader, epoch, max_batches, disp_interval=10):
            print('\nEpoch: %d' % epoch)
            model.train()
            train_loss = 0
            correct = 0
            total = 0

            for batch_idx, (inputs, targets) in enumerate(loader, start=1):
                if batch_idx >= max_batches:
                    break

                if self.use_cuda:
                    inputs, targets = inputs.cuda(), targets.cuda()
                optimizer.zero_grad()
                inputs, targets = Variable(inputs), Variable(targets)
                outputs = model(inputs)
                loss = criterion(outputs, targets)
                loss.backward()
                optimizer.step()

                train_loss += loss.data[0]
                _, predicted = torch.max(outputs.data, 1)
                total += targets.size(0)
                correct += predicted.eq(targets.data).cpu().sum()

                if batch_idx % disp_interval == 0 or batch_idx == len(loader):
                    progress_bar(batch_idx, len(loader), 'Loss: %.3f | Acc: %.3f%% (%d/%d)'
                             % (train_loss / batch_idx, 100. * correct / total, correct, total))
            return train_loss

        def test(loader, disp_interval=100):
            model.eval()
            test_loss = 0
            correct = 0
            total = 0
            for batch_idx, (inputs, targets) in enumerate(loader, start=1):
                if self.use_cuda:
                    inputs, targets = inputs.cuda(), targets.cuda()
                inputs, targets = Variable(inputs, volatile=True), Variable(targets)
                outputs = model(inputs)
                loss = criterion(outputs, targets)

                test_loss += loss.data[0]
                _, predicted = torch.max(outputs.data, 1)
                total += targets.size(0)
                correct += predicted.eq(targets.data).cpu().sum()

                if batch_idx % disp_interval == 0 or batch_idx == len(loader):
                    progress_bar(batch_idx, len(loader), 'Loss: %.3f | Acc: %.3f%% (%d/%d)'
                            % (test_loss / batch_idx, 100. * correct / total, correct, total))

            return correct / total

        # Train net for max_epochs
        for epoch in range(start_epoch, start_epoch+max_epochs):
            adjust_learning_rate(optimizer, epoch)
            train(train_loader, epoch, min(n_batches, batches_per_epoch))
            n_batches = n_batches - batches_per_epoch  # Decrement n_batches remaining

        # Evaluate trained net on val and test set
        val_acc = test(self.val_loader)
        test_acc = test(self.test_loader)

        # Save to file
        print("Saving file...")
        torch.save({
            'epoch': start_epoch+max_epochs,
            'model': model,
            'optimizer': optimizer,
            'val_error': 1-val_acc,
            'test_error': 1-test_acc,
        }, arm['filename'])

        return 1-val_acc, 1-test_acc

    def initialise_domain(self):
        params = {
            'learning_rate': Param('learning_rate', -6, 0, distrib='uniform', scale='log', logbase=10),
            'n_units_1': Param('n_units_1', 4, 8, distrib='uniform', scale='log', logbase=2, interval=1),
            'n_units_2': Param('n_units_2', 4, 8, distrib='uniform', scale='log', logbase=2, interval=1),
            'n_units_3': Param('n_units_3', 4, 8, distrib='uniform', scale='log', logbase=2, interval=1),
            'batch_size': Param('batch_size', 32, 512, distrib='uniform', scale='linear', interval=1),
            'lr_step': Param('lr_step', 1, 5, distrib='uniform', init_val=1, scale='linear', interval=1),
            'gamma': Param('gamma', -3, -1, distrib='uniform', init_val=0.1, scale='log', logbase=10),
            'weight_decay': Param('weight_decay', -6, -1, init_val=0.004, distrib='uniform', scale='log', logbase=10),
            'momentum': Param('momentum', 0.3, 0.999, init_val=0.9, distrib='uniform', scale='linear'),
        }
        return params