#!/bin/bash

#SBATCH --job-name=subv-isolate
#SBATCH --partition=exacloud
#SBATCH --verbose

#SBATCH --mem=1000
#SBATCH --time=500


# get the directory containing the experiment and the sub-directory where the
# subtypes enumareted during the setup step will be saved
export BASEDIR=HetMan/experiments/subvariant_isolate
source activate precepts

while getopts t:g:c:s:l:m: var
do
	case "$var" in
		t)	export cohort=$OPTARG;;
		g)	export gene=$OPTARG;;
		c)	export classif=$OPTARG;;
		s)	export samp_cutoff=$OPTARG;;
		l)	export mut_levels=$OPTARG;;
		m)	export test_max=$OPTARG;;
		[?])    echo "Usage: $0 [-t] TCGA cohort [-g] mutated gene" \
			     "[-c] mutation classifier [-s] minimum sample cutoff" \
			     "[-l] mutation annotation levels" \
			     "[-m] maximum tests per node";
			exit 1;;
	esac
done

export OUTDIR=$BASEDIR/output/$cohort/$gene/$classif/samps_${samp_cutoff}/$mut_levels
rm -rf $OUTDIR
mkdir -p $OUTDIR/slurm

# setup the experiment by finding a list of mutation subtypes to be tested
if [ ! -e ${BASEDIR}/setup/${cohort}/${gene}/mtypes_list__samps_${samp_cutoff}__levels_${mut_levels}.p ]
then

	srun -p=exacloud \
		--output=$BASEDIR/setup/slurm_${cohort}.txt \
		--error=$BASEDIR/setup/slurm_${cohort}.err \
		python $BASEDIR/setup_isolate.py -v \
		$cohort $gene $mut_levels --samp_cutoff=$samp_cutoff
fi

# find how large of a batch array to submit based on how many mutation types were
# found in the setup enumeration
mtypes_count=$(cat ${BASEDIR}/setup/${cohort}/${gene}/mtypes_count__samps_${samp_cutoff}__levels_${mut_levels}.txt)
export array_size=$(( $mtypes_count / $test_max ))

if [ $array_size -gt 199 ]
then
	export array_size=199
fi

# run the subtype tests in parallel
sbatch --array=0-$(( $array_size )) $BASEDIR/fit_isolate.sh

