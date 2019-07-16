
from dryadic.features.cohorts import BaseMutationCohort
from dryadic.features.cohorts.utils import (
    get_gencode, log_norm, drop_duplicate_genes)

import numpy as np
import pandas as pd


def load_beat_expression(expr_source, expr_file):
    if expr_source == 'toil__gns':
        expr_mat = pd.read_csv(expr_file, sep='\t', index_col=0)

    elif expr_source == 'toil__txs':
        expr_mat = pd.read_csv(expr_file, sep='\t', index_col=0)

        tx_gene = pd.read_csv(tx_map, sep='\t', index_col=0)
        expr_mat = expr_mat.loc[
            expr_mat.index.isin(tx_gene.index)].transpose()

        expr_mat.columns = pd.MultiIndex.from_arrays(
            [tx_gene.loc[expr_mat.columns]['gene'], expr_mat.columns],
            names=['Gene', 'Transcript']
            )

    return log_norm(expr_mat.fillna(0.0))


def load_beat_mutations(syn):
    return pd.read_csv(syn.get('syn18683049').path, sep='\t')


class BeatAmlCohort(BaseMutationCohort):

    def __init__(self,
                 mut_levels, mut_genes, expr_source, expr_file, samp_file,
                 syn, annot_file, domain_dir=None, cv_seed=None, test_prop=0,
                 **coh_args):
        self.cohort = 'beatAML'

        # TODO: incorporate supplemental mutation data, eg. laboratory-based
        # data for FLT3 ITDs found here:
        # https://www.nature.com/articles/s41586-018-0623-z

        expr = load_beat_expression(expr_source, expr_file)
        muts = load_beat_mutations(syn)
        samp_data = pd.read_csv(samp_file, sep='\t')

        muts['Sample'] = ['pid{}'.format(pid) for pid in muts.patient_id]
        samp_data['Sample'] = ['pid{}'.format(pid)
                               for pid in samp_data.patientId]
        use_samps = set(expr.index) & set(samp_data.Sample)

        expr = expr.loc[use_samps]
        muts = muts.loc[muts.Sample.isin(use_samps)]
        muts = muts.rename(columns={
            'symbol': 'Gene', 'chosen_consequence': 'Form', 'exon': 'Exon',
            'short_aa_change': 'Protein'
            })

        muts.Form = muts.Form.map({
            'missense_variant': 'Missense_Mutation',
            'frameshift_variant': 'Frame_Shift',
            'inframe_deletion': 'In_Frame_Del',
            'inframe_insertion': 'In_Frame_Ins',
            'stop_gained': 'Nonsense_Mutation',
            'start_lost': 'Nonsense_Mutation',
            'protein_altering_variant': 'Nonsense_Mutation',
            'stop_lost': 'Nonsense_Mutation',
            'internal_tandem_duplication': 'ITD',
            'splice_acceptor_variant': 'Splice_Site',
            'splice_donor_variant': 'Splice_Site',
            })

        annot_data = get_gencode(annot_file, ['transcript'])
        use_genes = set(expr.columns) & set(annot_data.keys())
        expr = expr[list(use_genes)]
        expr.columns = [annot_data[gn]['gene_name'] for gn in expr.columns] 
        expr = drop_duplicate_genes(expr)

        self.gene_annot = {at['gene_name']: {**{'Ens': ens}, **at}
                           for ens, at in annot_data.items()
                           if ens in use_genes}

        if 'Gene' in mut_levels:
            scale_lvl = mut_levels.index('Gene') + 1
        else:
            scale_lvl = 0

        muts = muts.loc[muts.Gene.isin(self.gene_annot.keys())]
        mut_levels.insert(scale_lvl, 'Scale')
        muts['Scale'] = 'Point'

        super().__init__(expr, muts, mut_levels, mut_genes,
                         domain_dir, cv_seed, test_prop)

