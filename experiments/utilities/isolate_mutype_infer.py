
import os
base_dir = os.path.dirname(__file__)

import sys
sys.path.extend([os.path.join(base_dir, '../../..')])

from HetMan.features.cohorts.tcga import MutationCohort
from HetMan.features.mutations import MuType
from HetMan.experiments.utilities.classifiers import *

import synapseclient
import argparse
from glob import glob
import dill as pickle

import pandas as pd
from importlib import import_module
from operator import or_
from functools import reduce

firehose_dir = "/home/exacloud/lustre1/share_your_data_here/precepts/firehose"


def main():
    """Runs the experiment."""

    parser = argparse.ArgumentParser(
        "Isolate the expression signature of mutation subtypes from their "
        "parent gene(s)' signature or that of a list of genes in a given "
        "TCGA cohort."
        )

    # positional command line arguments for where input data and output
    # data is to be stored
    parser.add_argument('mtype_file', type=str,
                        help='the pickle file where sub-types are stored')
    parser.add_argument('out_dir', type=str,
                        help='where to save the output of testing sub-types')

    # positional arguments for which cohort of samples and which mutation
    # classifier to use for testing
    parser.add_argument('cohort', type=str, help='a TCGA cohort')
    parser.add_argument('classif', type=str,
                        help='a classifier in HetMan.predict.classifiers')

    parser.add_argument('--use_genes', type=str, default=None, nargs='+',
                        help='specify which gene(s) to isolate against')

    parser.add_argument(
        '--cv_id', type=int, default=6732,
        help='the random seed to use for cross-validation draws'
        )

    parser.add_argument(
        '--task_count', type=int, default=10,
        help='how many parallel tasks the list of types to test is split into'
        )
    parser.add_argument('--task_id', type=int, default=0,
                        help='the subset of subtypes to assign to this task')

    # optional arguments controlling how classifier tuning is to be performed
    parser.add_argument(
        '--tune_splits', type=int, default=4,
        help='how many cohort splits to use for tuning'
        )
    parser.add_argument(
        '--test_count', type=int, default=16,
        help='how many hyper-parameter values to test in each tuning split'
        )

    parser.add_argument(
        '--infer_splits', type=int, default=20,
        help='how many cohort splits to use for inference bootstrapping'
        )
    parser.add_argument(
        '--infer_folds', type=int, default=4,
        help=('how many parts to split the cohort into in each inference '
              'cross-validation run')
        )

    parser.add_argument(
        '--parallel_jobs', type=int, default=4,
        help='how many parallel CPUs to allocate the tuning tests across'
        )

    parser.add_argument('--verbose', '-v', action='store_true',
                        help='turns on diagnostic messages')

    args = parser.parse_args()
    out_file = os.path.join(args.out_dir,
                            'out__task-{}.p'.format(args.task_id))

    if args.verbose:
        print("Starting isolation for sub-types in\n{}\nthe results of "
              "which will be stored in\n{}\nwith classifier <{}>.".format(
                  args.mtype_file, args.out_dir, args.classif
                ))

    mtype_list = pickle.load(open(args.mtype_file, 'rb'))
    use_lvls = []

    for lvls in reduce(or_, [{mtype.get_sorted_levels()}
                             for mtype in mtype_list]):
        for lvl in lvls:
            if lvl not in use_lvls:
                use_lvls.append(lvl)

    if args.use_genes is None:
        if set(mtype.cur_level for mtype in mtype_list) == {'Gene'}:
            use_genes = reduce(or_, [set(gn for gn, _ in mtype.subtype_list())
                                     for mtype in mtype_list])

        else:
            raise ValueError(
                "A gene to isolate against must be given or the subtypes "
                "listed must have <Gene> as their top level!"
                )

    else:
        use_genes = set(args.use_genes)

    if args.verbose:
        print("Subtypes at mutation annotation levels {} will be isolated "
              "against genes:\n{}".format(use_lvls, use_genes))

    # log into Synapse using locally stored credentials
    syn = synapseclient.Synapse()
    syn.cache.cache_root_dir = ("/home/exacloud/lustre1/CompBio/"
                                "mgrzad/input-data/synapse")
    syn.login()

    # loads the expression data and gene mutation data for the given TCGA
    # cohort, with the training/testing cohort split defined by the
    # cross-validation ID for this task
    cdata = MutationCohort(
        cohort=args.cohort, mut_genes=list(use_genes), mut_levels=use_lvls,
        expr_source='Firehose', expr_dir=firehose_dir,
        syn=syn, cv_seed=args.cv_id, cv_prop=1.0
        )

    if args.verbose:
        print("Loaded {} subtypes of which roughly {} will be isolated in "
              "cohort {} with {} samples.".format(
                  len(mtype_list), len(mtype_list) // args.task_count,
                  args.cohort, len(cdata.samples)
                ))

    mut_clf = eval(args.classif)
    out_tune = {mtype: {par: None for par, _ in mut_clf.tune_priors}
                for mtype in mtype_list}
    out_iso = {mtype: None for mtype in mtype_list}

    base_mtype = MuType({('Gene', tuple(use_genes)): None})
    base_samps = base_mtype.get_samples(cdata.train_mut)

    # for each subtype, check if it has been assigned to this task
    for i, mtype in enumerate(mtype_list):
        if (i % args.task_count) == args.task_id:
            if args.verbose:
                print("Isolating {} ...".format(mtype))

            clf = mut_clf()
            ex_samps = base_samps - mtype.get_samples(cdata.train_mut)

            clf.tune_coh(
                cdata, mtype,
                exclude_genes=use_genes, exclude_samps=ex_samps,
                tune_splits=args.tune_splits, test_count=args.test_count,
                parallel_jobs=args.parallel_jobs
                )

            clf_params = clf.get_params()
            for par, _ in mut_clf.tune_priors:
                out_tune[mtype][par] = clf_params[par]
 
            out_iso[mtype] = clf.infer_coh(
                cdata, mtype,
                exclude_genes=use_genes, force_test_samps=ex_samps,
                infer_splits=args.infer_splits, infer_folds=args.infer_folds,
                parallel_jobs=args.parallel_jobs
                )

        else:
            del(out_iso[mtype])
            del(out_tune[mtype])

    pickle.dump(
        {'Infer': out_iso, 'Tune': out_tune,
         'Info': {'Clf': mut_clf,
                  'TuneSplits': args.tune_splits,
                  'TestCount': args.test_count,
                  'InferFolds': args.infer_folds}},
        open(out_file, 'wb')
        )


if __name__ == "__main__":
    main()

