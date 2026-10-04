import unittest
import feature_groups
from test_optimize import defaults
class FeatureGroupsTests(unittest.TestCase):
    def test_control_and_new_feature(self):
        c=defaults();g=feature_groups.classifier_config(c)
        self.assertEqual([f['name'] for f in g['feature_group']['features']],[f['name'] for f in c['features']])
        g=feature_groups.classifier_config(c,'classic-rvol',period=14,window=20)
        self.assertEqual(g['feature_group']['features'][-1],{'name':'RVOL','a':14,'b':1,'normalization':'rolling-z','window':20})
        self.assertNotIn('normalization',c['features'][0])
    def test_no_arbitrary_permutation_or_duplicate(self):
        with self.assertRaises(ValueError):feature_groups.classifier_config(defaults(),'rvol-classic')
        c=defaults();c['features'][1]=c['features'][0]
        with self.assertRaises(ValueError):feature_groups.classifier_config(c)
    def test_explicit_model_parameters(self):
        self.assertEqual(feature_groups.model_config()['vote_weight'],'equal')
        self.assertEqual(feature_groups.model_config(vote_weight='inverse',vote_half_life=50)['vote_half_life'],50)
        for kwargs in ({'epsilon':1e-6},{'vote_half_life':.5},{'vote_half_life':10,'rank_half_life':10},{'epsilon':float('inf')}):
            with self.assertRaises(ValueError):feature_groups.model_config(**kwargs)
