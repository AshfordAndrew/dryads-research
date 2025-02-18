
from ..utilities.pipeline_setup import get_task_count
from ..utilities.misc import compare_muts
from ..utilities.metrics import calc_auc
from .utils import load_scRNA_expr

import os
import argparse
import bz2
from pathlib import Path
import dill as pickle
from joblib import Parallel, delayed
import random

import numpy as np
import pandas as pd

from itertools import cycle, product
from itertools import combinations as combn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('use_dir', type=str)
    parser.add_argument('--task_ids', type=int, nargs='+')
    args = parser.parse_args()

    # load the -omic datasets for this experiment's cohorts
    sc_expr = load_scRNA_expr()
    with bz2.BZ2File(os.path.join(args.use_dir, 'setup',
                                  "cohort-data.p.gz"), 'r') as f:
        cdata = pickle.load(f)

    with open(os.path.join(args.use_dir, 'setup', "muts-list.p"), 'rb') as f:
        muts_list = pickle.load(f)
    with open(os.path.join(args.use_dir, 'setup', "feat-list.p"), 'rb') as f:
        use_feats = pickle.load(f)

    # get list of output files from all parallelized jobs
    file_list = tuple(Path(args.use_dir, 'output').glob("out__cv-*_task*.p"))
    file_dict = dict()

    # filter output files according to whether they came from one of the
    # parallelized tasks assigned to this gather task
    for out_fl in file_list:
        fl_info = out_fl.stem.split("out__")[1]
        out_task = int(fl_info.split("task-")[1])

        # gets the parallelized task id and learning cross-validation fold
        # each output file corresponds to
        if args.task_ids is None or out_task in args.task_ids:
            out_cv = int(fl_info.split("cv-")[1].split("_")[0])
            file_dict[out_fl] = out_task, out_cv

    # find the number of parallelized tasks used in this run of the pipeline
    assert (len(file_dict) % 40) == 0, "Missing output files detected!"
    task_count = get_task_count(args.use_dir)

    if args.task_ids is None:
        use_tasks = set(range(task_count))
        out_tag = ''

    else:
        use_tasks = set(args.task_ids)
        out_tag = "_{}".format('-'.join([
            str(tsk) for tsk in sorted(use_tasks)]))

    # organize output files according to their cross-validation fold for
    # easier collation of output data across parallelized task ids
    file_sets = {
        cv_id: {out_fl for out_fl, (out_task, out_cv) in file_dict.items()
                if out_task in use_tasks and out_cv == cv_id}
        for cv_id in range(40)
        }

    # initialize object that will store raw experiment output data
    out_dfs = {k: [None for cv_id in range(40)]
               for k in ['Pred', 'Pars', 'Time', 'Acc', 'Coef', 'SC']}
    out_clf = None
    out_tune = None

    random.seed(10301)
    random.shuffle(muts_list)

    use_muts = [mut for i, mut in enumerate(muts_list)
                if i % task_count in use_tasks]

    for cv_id, out_fls in file_sets.items():
        out_list = []

        for out_fl in out_fls:
            with open(out_fl, 'rb') as f:
                out_list += [pickle.load(f)]

        for out_dicts in out_list:
            if out_clf is None:
                out_clf = out_dicts['Clf']

            else:
                assert out_clf == out_dicts['Clf'], (
                    "Each experiment must be run with the same classifier!")

            if out_tune is None:
                out_tune = out_dicts['Clf'].tune_priors

            else:
                assert out_tune == out_dicts['Clf'].tune_priors, (
                    "Each experiment must be run with exactly "
                    "one set of tuning priors!"
                    )

        out_dfs['Coef'][cv_id] = pd.DataFrame({
            mut: out_vals for out_dicts in out_list
            for mut, out_vals in out_dicts['Coef'].items()
            }).transpose().fillna(0.0)

        out_dfs['Coef'][cv_id] = out_dfs['Coef'][cv_id].assign(
            **{gene: 0
               for gene in use_feats - set(out_dfs['Coef'][cv_id].columns)}
            )

        out_dfs['SC'][cv_id] = pd.DataFrame({
            mut: out_vals for out_dicts in out_list
            for mut, out_vals in out_dicts['SC'].items()
            }).transpose()

        for k in set(out_dfs.keys()) - {'Coef', 'SC'}:
            out_dfs[k][cv_id] = pd.concat([
                pd.DataFrame.from_dict(out_dicts[k], orient='index')
                for out_dicts in out_list
                ])

            assert sorted(out_dfs[k][cv_id].index) == sorted(use_muts), (
                "Mutations with predictions for c-v fold <{}> don't "
                "match those enumerated during setup!".format(cv_id)
            )

        # recover the cohort training/testing data split that was
        # used to generate the results in this file
        cdata_samps = sorted(cdata.get_samples())
        random.seed((cv_id // 4) * 7712 + 13)
        random.shuffle(cdata_samps)

        cdata.update_split(9073 + 97 * cv_id,
                           test_samps=cdata_samps[(cv_id % 4)::4])
        test_samps = cdata.get_test_samples()

        out_dfs['Pred'][cv_id].columns = test_samps
        out_dfs['SC'][cv_id].columns = sc_expr.index

    pred_df = pd.concat(out_dfs['Pred'], axis=1)
    assert all(smp in pred_df.columns for smp in cdata.get_samples()), (
        "Missing mutation scores for some samples in the cohort!")
    assert (pred_df.columns.value_counts() == 10).all(), (
        "Inconsistent number of CV scores across cohort samples!")

    sc_df = pd.concat(out_dfs['SC'], axis=1)
    assert (pred_df.columns.value_counts() == 10).all(), (
        "Inconsistent number of CV scores across cohort samples!")

    pars_df = pd.concat(out_dfs['Pars'], axis=1)
    assert pars_df.shape[1] == (40 * len(out_clf.tune_priors)), (
        "Tuned parameter values missing for some CVs!")

    time_df = pd.concat(out_dfs['Time'], axis=1)
    assert time_df.shape[1] == 80, (
        "Algorithm fitting times missing for some CVs!")
    assert (time_df.applymap(len) == out_clf.test_count).values.all(), (
        "Algorithm fitting times missing for some hyper-parameter values!")

    acc_df = pd.concat(out_dfs['Acc'], axis=1)
    assert acc_df.shape[1] == 120, (
        "Algorithm tuning accuracies missing for some CVs!")
    assert (acc_df.applymap(len) == out_clf.test_count).values.all(), (
        "Algorithm tuning stats missing for some hyper-parameter values!")

    coef_df = pd.concat(out_dfs['Coef'], axis=1)
    assert (coef_df.columns.value_counts() == 40).all(), (
        "Inconsistent number of model coefficients across cv-folds!")

    for out_df in [pred_df, pars_df, time_df, acc_df, coef_df, sc_df]:
        assert compare_muts(out_df.index, use_muts), (
            "Mutations for which predictions were made do not match the list "
            "of mutations enumerated during setup!"
        )

    with bz2.BZ2File(os.path.join(args.use_dir, 'merge',
                                  "out-coef{}.p.gz".format(out_tag)),
                     'w') as fl:
        pickle.dump(coef_df, fl, protocol=-1)

    pred_df = pd.DataFrame({
        mtype: pred_df.loc[mtype].groupby(level=0).apply(lambda x: x.values)
        for mtype in use_muts
        }).transpose()
    sc_df = pd.DataFrame({
        mtype: sc_df.loc[mtype].groupby(level=0).apply(lambda x: x.values)
        for mtype in use_muts
        }).transpose()

    assert (pred_df.applymap(len) == 10).values.all(), (
        "Incorrect number of testing CV scores!")

    with bz2.BZ2File(os.path.join(args.use_dir, 'merge',
                                  "out-pred{}.p.gz".format(out_tag)),
                     'w') as fl:
        pickle.dump(pred_df, fl, protocol=-1)

    with bz2.BZ2File(os.path.join(args.use_dir, 'merge',
                                  "out-sc{}.p.gz".format(out_tag)),
                     'w') as fl:
        pickle.dump(sc_df, fl, protocol=-1)

    with bz2.BZ2File(os.path.join(args.use_dir, 'merge',
                                  "out-tune{}.p.gz".format(out_tag)),
                     'w') as fl:
        pickle.dump([pars_df, time_df, acc_df, out_clf], fl, protocol=-1)

    cdata.update_split(test_prop=0)
    train_samps = np.array(cdata.get_train_samples())
    pheno_dict = {mtype: np.array(cdata.train_pheno(mtype))
                  for mtype in use_muts}

    with bz2.BZ2File(os.path.join(args.use_dir, 'merge',
                                  "out-pheno{}.p.gz".format(out_tag)),
                     'w') as fl:
        pickle.dump(pheno_dict, fl, protocol=-1)

    # calculates AUCs for prediction tasks using scores from all
    # cross-validations concatenated together...
    auc_dict = {
        'all': pd.Series(dict(zip(use_muts, Parallel(
            n_jobs=12, prefer='threads', pre_dispatch=120)(
            delayed(calc_auc)(
                pheno_dict[mtype],
                np.vstack(pred_df.loc[mtype][train_samps].values)
                )
            for mtype in use_muts
            )
        ))),

        # ...and for each cross-validation run considered separately...
        'CV': pd.DataFrame.from_records(
            tuple(zip(cycle(use_muts), Parallel(
                n_jobs=12, prefer='threads', pre_dispatch=120)(
                delayed(calc_auc)(
                    pheno_dict[mtype],
                    np.vstack(pred_df.loc[
                                  mtype][train_samps].values)[:, cv_id]
                    )
                for cv_id in range(10) for mtype in use_muts
                )
                      ))
            ).pivot_table(index=0, values=1, aggfunc=list).iloc[:, 0],

        # ...and finally using the average of predicted scores for each
        # sample across CV runs
        'mean': pd.Series(dict(zip(use_muts, Parallel(
            n_jobs=12, prefer='threads', pre_dispatch=120)(
                delayed(calc_auc)(
                    pheno_dict[mtype],
                    np.vstack(pred_df.loc[
                                  mtype][train_samps].values).mean(axis=1)
                    )
                for mtype in use_muts
                )
            )))
        }

    auc_dict['CV'].name = None
    auc_dict['CV'].index.name = None

    with bz2.BZ2File(os.path.join(args.use_dir, 'merge',
                                  "out-aucs{}.p.gz".format(out_tag)),
                     'w') as fl:
        pickle.dump(auc_dict, fl, protocol=-1)

    random.seed(7609)
    sub_inds = [random.choices([False, True], k=len(cdata.get_samples()))
                for _ in range(1000)]

    conf_df = pd.DataFrame.from_records(
        tuple(zip(cycle(use_muts), Parallel(
            n_jobs=12, prefer='threads', pre_dispatch=120)(
            delayed(calc_auc)(
                pheno_dict[mtype][sub_indx],
                np.vstack(pred_df.loc[
                              mtype][train_samps[sub_indx]].values).mean(
                    axis=1)
                )
            for sub_indx in sub_inds for mtype in use_muts
            )
                  ))
        ).pivot_table(index=0, values=1, aggfunc=list).iloc[:, 0]

    conf_df.name = None
    conf_df.index.name = None

    with bz2.BZ2File(os.path.join(args.use_dir, 'merge',
                                  "out-conf{}.p.gz".format(out_tag)),
                     'w') as fl:
        pickle.dump(conf_df, fl, protocol=-1)


if __name__ == "__main__":
    main()

