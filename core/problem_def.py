import pprint
import abc


class Problem(object):

    __metaclass__ = abc.ABCMeta

    def __init__(self):
        self.domain = None

    def generate_random_arm(self, hps=None):
        if not hps:
            hps = self.domain.keys()
        arm = {}
        for hp in self.domain.keys():
            # if sample is required, draw for param range
            if hp in hps:
                val = self.domain[hp].get_param_range(1, stochastic=True)
                arm[hp] = val[0]
            # else set to default
            else:
                val = self.domain[hp].init_val
                assert val is not None, "No default value is set for param {}".format(hp)
                arm[hp] = val
        return arm

    def print_domain(self):
        pp = pprint.PrettyPrinter(indent=4)
        pp.pprint(self.domain)

    @abc.abstractmethod
    def initialise_objective_function(self, x):
        pass

    @abc.abstractmethod
    def initialise_domain(self):
        pass

