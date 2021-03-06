#!/usr/bin/env python2.7
"""
vg_mapeval.py: Compare alignment positions from gam or bam to a truth set
that was created with vg sim --gam

"""
from __future__ import print_function
import argparse, sys, os, os.path, errno, random, subprocess, shutil, itertools, glob, tarfile
import doctest, re, json, collections, time, timeit
import logging, logging.handlers, SocketServer, struct, socket, threading
import string, math
import urlparse
import getpass
import pdb
import gzip
import logging
import copy
from collections import Counter

from math import ceil
from subprocess import Popen, PIPE

try:
    import numpy as np
    from sklearn.metrics import roc_auc_score, average_precision_score, r2_score
    have_sklearn = True
except:
    have_sklearn = False

import tsv

from toil.common import Toil
from toil.job import Job
from toil.realtimeLogger import RealtimeLogger
from toil_vg.vg_common import require, make_url, \
    write_to_store, add_common_vg_parse_args, add_container_tool_parse_args
from toil_vg.vg_map import map_parse_args, run_mapping
from toil_vg.vg_index import run_indexing
from toil_vg.context import Context

logger = logging.getLogger(__name__)

def mapeval_subparser(parser):
    """
    Create a subparser for mapeval.  Should pass in results of subparsers.add_parser()
    """

    # Add the Toil options so the job store is the first argument
    Job.Runner.addToilOptions(parser)
    
    # Add the out_store
    # TODO: do this at a higher level?
    # Or roll into Context?
    parser.add_argument('out_store',
                        help='output store.  All output written here. Path specified using same syntax as toil jobStore')
    
    # Add mapeval stuff
    add_mapeval_options(parser)
    
    # Add common docker options
    add_container_tool_parse_args(parser)
    
def add_mapeval_options(parser):
    """
    Add the mapeval options to the given argparse parser.
    """
    
    # General options
    parser.add_argument('truth', type=make_url, default=None,
                        help='list of true positions of reads as output by toil-vg sim')        
    parser.add_argument('--gams', nargs='+', type=make_url, default=[],
                        help='aligned reads to compare to truth.  specify xg index locations with --index-bases')
    parser.add_argument("--index-bases", nargs='+', type=make_url, default=[],
                        help='use in place of gams to perform alignment.  will expect '
                        '<index-base>.gcsa, <index-base>.lcb and <index-base>.xg to exist')
    parser.add_argument('--vg-graphs', nargs='+', type=make_url, default=[],
                        help='vg graphs to use in place of gams or indexes.  indexes'
                        ' will be built as required')
    parser.add_argument('--gam-names', nargs='+', default=[],
                        help='a name for each gam passed in --gams/graphs/index-bases')
    parser.add_argument('--bams', nargs='+', type=make_url, default=[],
                        help='aligned reads to compare to truth in BAM format')
    parser.add_argument('--bam-names', nargs='+', default=[],
                        help='a name for each bam passed in with --bams')
    parser.add_argument('--pe-bams', nargs='+', type=make_url, default=[],
                        help='paired end aligned reads t compare to truth in BAM format')
    parser.add_argument('--pe-bam-names', nargs='+', default=[],
                        help='a name for each bam passed in with --pe-bams')
    parser.add_argument('--vg-paired', action='store_true',
                        help='if running vg map, do vg map -i as well')
    
    parser.add_argument('--mapeval-threshold', type=int, default=100,
                        help='distance between alignment and true position to be called correct')

    parser.add_argument('--bwa', action='store_true',
                        help='run bwa mem on the reads, and add to comparison')
    parser.add_argument('--bwa-paired', action='store_true',
                        help='run bwa mem paired end as well')
    parser.add_argument('--fasta', type=make_url, default=None,
                        help='fasta sequence file (required for bwa)')
    parser.add_argument('--gam-reads', type=make_url, default=None,
                        help='reads in GAM format (required for bwa)')

    parser.add_argument('--bwa-opts', type=str,
                        help='arguments for bwa mem (wrapped in \"\").')
    
    # We can compare all the scores against those from a particular GAM, if asked.
    parser.add_argument('--compare-gam-scores', default=None,
                        help='compare scores against those in the given named GAM')
                        
    # We also need to have these options to make lower-level toil-vg code happy
    # with the options namespace we hand it.
    
    # Add mapping options
    map_parse_args(parser)
    
    # Add common options shared with everybody
    add_common_vg_parse_args(parser)
    
def get_default_mapeval_options(truth):
    """
    Return an argparse Namespace populated with the default mapeval option
    values.
    
    Requires the required positional truth file argument.
    
    Can be modified and then passed to make_mapeval_plan(), so you can use
    mapeval as part of a larger program.
    
    """
    
    # Make a parser
    parser = argparse.ArgumentParser()
    # Stick our arguments on it
    add_mapeval_options(parser)
    # And parse nothing but mandatory arguments
    return parser.parse_args([truth])
    
def validate_options(options):
    """
    Throw an error if an invalid combination of options has been selected.
    """
    
    # check bwa / bam input parameters.  
    if options.bwa or options.bwa_paired:
        require(options.gam_input_reads, '--gam_input_reads required for bwa')
        require(options.fasta, '--fasta required for bwa')
    if options.bams:
        require(options.bam_names and len(options.bams) == len(options.bam_names),
                 '--bams and --bam-names must have same number of inputs')
    if options.pe_bams:
        require(options.pe_bam_names and len(options.pe_bams) == len(options.pe_bam_names),
                 '--pe-bams and --pe-bam-names must have same number of inputs')

    # some options from toil-vg map are disabled on the command line
    # this can be eventually cleaned up a bit better 
    require(not (options.interleaved or options.fastq),
            '--interleaved and --fastq disabled in toil-vg mapeval')

    # accept graphs or indexes in place of gams
    require(options.gams or options.index_bases or options.vg_graphs,
            'one of --vg-graphs, --index-bases or --gams must be used to specifiy vg input')

    if options.gams:
        require(len(options.index_bases) == len(options.gams),
                '--index-bases must be used along with --gams to specify xg locations')
    if options.vg_graphs:
        require(not options.gams and not options.index_bases,
                'if --vg-graphs specified, --gams and --index-bases must not be used')

    # must have a name for each graph/index/gam
    if options.gams:
        require(options.gam_names and len(options.gams) == len(options.gam_names),
                 '--gams and --gam_names must have same number of inputs')
    if options.vg_graphs:
        require(options.gam_names and len(options.vg_graphs) == len(options.gam_names),
                 '--vg-graphs and --gam_names must have same number of inputs')
    if options.index_bases:
        require(options.gam_names and len(options.index_bases) == len(options.gam_names),
                 '--index-bases and --gam_names must have same number of inputs')
                 
    # Make sure names are unique so we can use them as dict keys and file names
    names = []
    if options.bam_names:
        names += options.bam_names
    if options.pe_bam_names:
        names += options.pe_bam_names
    if options.gam_names:
        names += options.gam_names
    require(len(names) == len(set(names)), 'all names must be unique')
    
def parse_int(value):
    """
    Parse an int, interpreting an empty string as 0.
    """
    
    return int(value) if value.strip() != '' else 0

def run_bwa_index(job, context, do_bwa, do_bwa_paired, gam_file_id, fasta_file_id, bwa_index_ids):
    """
    Make a bwa index for a fast sequence if not given in input. then run bwa mem
    
    Retuns a pair of BAM IDs, one for unpaired and one for paired. Either will be None if the corresponding flag is false.
    """
    if not bwa_index_ids:
        bwa_index_ids = dict()
        work_dir = job.fileStore.getLocalTempDir()
        # Download the FASTA file to be indexed
        # It would be nice to name it the same as the actual input FASTA but we'd have to peek at the options
        fasta_file = os.path.join(work_dir, 'toindex.fa')
        job.fileStore.readGlobalFile(fasta_file_id, fasta_file)
        cmd = ['bwa', 'index', os.path.basename(fasta_file)]
        context.runner.call(job, cmd, work_dir = work_dir)
        for idx_file in glob.glob('{}.*'.format(fasta_file)):
            # Upload all the index files created, and store their IDs under their extensions
            bwa_index_ids[idx_file[len(fasta_file):]] = context.write_intermediate_file(job, idx_file)

    bwa_stats_file_id = None
    bwa_pair_stats_file_id = None
                    
    if do_bwa:
        bwa_stats_file_id = job.addChildJobFn(run_bwa_mem, context, gam_file_id, bwa_index_ids, False,
                                            cores=context.config.alignment_cores, memory=context.config.alignment_mem,
                                            disk=context.config.alignment_disk).rv()
    if do_bwa_paired:
        bwa_pair_stats_file_id = job.addChildJobFn(run_bwa_mem, context, gam_file_id, bwa_index_ids, True,
                                                 cores=context.config.alignment_cores, memory=context.config.alignment_mem,
                                                 disk=context.config.alignment_disk).rv()

    return bwa_stats_file_id, bwa_pair_stats_file_id

    
def run_bwa_mem(job, context, gam_file_id, bwa_index_ids, paired_mode):
    """ run bwa-mem on reads in a gam.  optionally run in paired mode
    return id of bam file
    """

    work_dir = job.fileStore.getLocalTempDir()

    # read the gam file
    gam_file = os.path.join(work_dir, 'input.gam')
    job.fileStore.readGlobalFile(gam_file_id, gam_file)

    # and the index files
    fasta_file = os.path.join(work_dir, 'reference.fa')
    for suf, idx_id in bwa_index_ids.items():
        job.fileStore.readGlobalFile(idx_id, '{}{}'.format(fasta_file, suf))

    # output positions file
    bam_file = os.path.join(work_dir, 'bwa-mem')
    if paired_mode:
        bam_file += '-pe'
    bam_file += '.bam'
    
    # if we're paired, must make some split files
    if paired_mode:

        # convert to json (todo: have docker image that can do vg and jq)
        json_file = gam_file + '.json'
        cmd = ['vg', 'view', '-a', os.path.basename(gam_file)]
        with open(json_file, 'w') as out_json:
            context.runner.call(job, cmd, work_dir = work_dir, outfile = out_json)

        sim_fq_files = [None, os.path.join(work_dir, 'sim_1.fq.gz'),
                        os.path.join(work_dir, 'sim_2.fq.gz')]

        # make a fastq for each end of pair
        for i in [1, 2]:
            # extract paired end with jq
            cmd = ['jq', '-cr', 'select(.name | test("_{}$"))'.format(i),
                   os.path.basename(json_file)]
            end_file = json_file + '.{}'.format(i)
            with open(end_file, 'w') as end_out:
                context.runner.call(job, cmd, work_dir = work_dir, outfile = end_out)

            cmd = [['vg', 'view', '-JaG', os.path.basename(end_file)]]
            cmd.append(['vg', 'view', '-X', '-'])
            cmd.append(['sed', 's/_{}$//'.format(i)])
            cmd.append(['gzip'])

            with open(sim_fq_files[i], 'w') as sim_out:
                context.runner.call(job, cmd, work_dir = work_dir, outfile = sim_out)

            os.remove(end_file)

        # run bwa-mem on the paired end input
        cmd = ['bwa', 'mem', '-t', str(context.config.alignment_cores), os.path.basename(fasta_file),
                os.path.basename(sim_fq_files[1]), os.path.basename(sim_fq_files[2])] + context.config.bwa_opts        
        with open(bam_file + '.sam', 'w') as out_sam:
            context.runner.call(job, cmd, work_dir = work_dir, outfile = out_sam)

        # separate samtools for docker (todo find image with both)
        # 2304 = get rid of 256 (secondary) + 2048 (supplementary)        
        cmd = ['samtools', 'view', '-1', '-F', '2304', os.path.basename(bam_file + '.sam')]
        with open(bam_file, 'w') as out_bam:
            context.runner.call(job, cmd, work_dir = work_dir, outfile = out_bam)

    # single end
    else:

        # extract reads from gam.  as above, need to have single docker container (which shouldn't be
        # a big deal) to run all these chained command and avoid huge files on disk
        extracted_reads_file = os.path.join(work_dir, 'extracted_reads')
        cmd = ['vg', 'view', '-X', os.path.basename(gam_file)]
        with open(extracted_reads_file, 'w') as out_ext:
            context.runner.call(job, cmd, work_dir = work_dir, outfile = out_ext)

        # run bwa-mem on single end input
        cmd = ['bwa', 'mem', '-t', str(context.config.alignment_cores), os.path.basename(fasta_file),
                os.path.basename(extracted_reads_file)] + context.config.bwa_opts

        with open(bam_file + '.sam', 'w') as out_sam:
            context.runner.call(job, cmd, work_dir = work_dir, outfile = out_sam)

        # separate samtools for docker (todo find image with both)
        # 2304 = get rid of 256 (secondary) + 2048 (supplementary)
        cmd = ['samtools', 'view', '-1', '-F', '2304', os.path.basename(bam_file + '.sam')]
        with open(bam_file, 'w') as out_bam:
            context.runner.call(job, cmd, work_dir = work_dir, outfile = out_bam) 

    # return our id for the output bam file
    bam_file_id = context.write_intermediate_file(job, bam_file)

    return bam_file_id

def extract_bam_read_stats(job, context, name, bam_file_id, paired):
    """
    extract positions, scores, and MAPQs from bam, return id of read stats file
    (lots of duplicated code with vg_sim, should merge?)
    
    Produces a read stats TSV of the format:
    read name, contig aligned to, alignment position, score, MAPQ
    
    TODO: Currently scores are not extracted and a score of 0 is always
    returned.

    """

    work_dir = job.fileStore.getLocalTempDir()

    # download input
    bam_file = os.path.join(work_dir, name)
    job.fileStore.readGlobalFile(bam_file_id, bam_file)

    out_pos_file = bam_file + '.tsv'

    cmd = [['samtools', 'view', os.path.basename(bam_file)]]
    cmd.append(['grep', '-v', '^@'])
    if paired:
        # Now we use inline perl to parse the SAM flags and synthesize TSV
        # TODO: will need to switch to something more powerful to parse the score out of the AS tag. For now score everything as 0.
        # TODO: why _ and not / as the read name vs end number delimiter?
        cmd.append(['perl', '-ne', '@val = split("\t", $_); print @val[0] . "_" . (@val[1] & 64 ? "1" : @val[1] & 128 ? "2" : "?"), "\t" . @val[2] . "\t" . @val[3] . "\t0\t" . @val[4] . "\n";'])
    else:
        # No flags to parse since there's no end pairing and read names are correct.
        # Use inline perl again and insert a fake 0 score column
        cmd.append(['perl', '-ne', '@val = split("\t", $_); print @val[0] . "\t" . @val[2] . "\t" . @val[3] . "\t0\t" . @val[4] . "\n";'])
    cmd.append(['sort'])
    
    with open(out_pos_file, 'w') as out_pos:
        context.runner.call(job, cmd, work_dir = work_dir, outfile = out_pos)

    stats_file_id = context.write_intermediate_file(job, out_pos_file)
    return stats_file_id

    
def annotate_gam(job, context, xg_file_id, gam_file_id):
    """
    Annotate the given GAM file with positions from the given XG file.
    """
    
    work_dir = job.fileStore.getLocalTempDir()

    # download input
    RealtimeLogger.info('Download XG from file {}'.format(xg_file_id))
    xg_file = os.path.join(work_dir, 'index.xg')
    job.fileStore.readGlobalFile(xg_file_id, xg_file)
    gam_file = os.path.join(work_dir, 'reads.gam')
    job.fileStore.readGlobalFile(gam_file_id, gam_file)
    
    annotated_gam_file = os.path.join(work_dir, 'annotated.gam')
    
    cmd = [['vg', 'annotate', '-p', '-a', os.path.basename(gam_file), '-x', os.path.basename(xg_file)]]
    with open(annotated_gam_file, 'w') as out_file:
        context.runner.call(job, cmd, work_dir=work_dir, outfile=out_file)
    
    return context.write_intermediate_file(job, annotated_gam_file)
    
    
def extract_gam_read_stats(job, context, name, gam_file_id):
    """
    extract positions, scores, and MAPQs for reads from a gam, and return the id
    of the resulting read stats file
    
    Produces a read stats TSV of the format:
    read name, contig aligned to, alignment position, score, MAPQ
    
    If the GAM is not annotated with alignment positions, contig and position
    will both contain only "0" values.

    """

    work_dir = job.fileStore.getLocalTempDir()

    gam_file = os.path.join(work_dir, name)
    job.fileStore.readGlobalFile(gam_file_id, gam_file)

    out_pos_file = gam_file + '.tsv'
                           
    # go through intermediate json file until docker worked out
    gam_annot_json = gam_file + '.json'
    cmd = [['vg', 'view', '-aj', os.path.basename(gam_file)]]
    with open(gam_annot_json, 'w') as output_annot_json:
        context.runner.call(job, cmd, work_dir = work_dir, outfile=output_annot_json)

    # turn the annotated gam json into truth positions, as separate command since
    # we're going to use a different docker container.  (Note, would be nice to
    # avoid writing the json to disk)        
    jq_cmd = [['jq', '-c', '-r', '[.name, .refpos[0].name, .refpos[0].offset, .score,'
               'if .mapping_quality == null then 0 else .mapping_quality end ] | @tsv',
               os.path.basename(gam_annot_json)]]
    jq_cmd.append(['sed', 's/null/0/g'])

    with open(out_pos_file + '.unsorted', 'w') as out_pos:
        context.runner.call(job, jq_cmd, work_dir = work_dir, outfile=out_pos)

    # get rid of that big json asap
    os.remove(gam_annot_json)

    # sort the read stats file (not piping due to memory fears)
    sort_cmd = ['sort', os.path.basename(out_pos_file) + '.unsorted']
    with open(out_pos_file, 'w') as out_pos:
        context.runner.call(job, sort_cmd, work_dir = work_dir, outfile = out_pos)

    # Make sure each line has all columns
    RealtimeLogger.info("Make sure all lines are full length")
    context.runner.call(job, ['awk', '!length($5)',  os.path.basename(out_pos_file)], work_dir = work_dir)

    out_stats_file_id = context.write_intermediate_file(job, out_pos_file)
    return out_stats_file_id
    
def compare_positions(job, context, truth_file_id, name, stats_file_id, mapeval_threshold):
    """
    this is essentially pos_compare.py from vg/scripts
    return output file id.
    
    Compares positions from two TSV files. The truth has the format:
    read name, contig simulated from, true position
    
    And the file under test is a read stats TSV with the format:
    read name, contig aligned to, alignment position, alignment score, MAPQ
    
    Produces a CSV (NOT TSV) of the form:
    read name, correctness flag (0/1), MAPQ
    
    mapeval_threshold is the distance within which a mapping is held to have hit
    the correct position.
    
    """
    work_dir = job.fileStore.getLocalTempDir()

    true_read_stats_file = os.path.join(work_dir, 'true.tsv')
    job.fileStore.readGlobalFile(truth_file_id, true_read_stats_file)
    test_read_stats_file = os.path.join(work_dir, name + '.tsv')
    job.fileStore.readGlobalFile(stats_file_id, test_read_stats_file)

    out_file = os.path.join(work_dir, name + '.compare.positions')

    with open(true_read_stats_file) as truth, open(test_read_stats_file) as test, \
         open(out_file, 'w') as out:
        line_no = 0
        for true_fields, test_fields in itertools.izip(tsv.TsvReader(truth), tsv.TsvReader(test)):
            # Zip everything up and assume that the reads correspond
            line_no += 1
            # every input has a true position
            true_read_name = true_fields[0]
            if len(true_fields) != 3 or len(test_fields) != 5:
                # With the new TSV reader, the files should always have the
                # correct field counts. Some fields just might be empty.
                raise RuntimeError('Incorrect field counts on line {} for {}: {} and {}'.format(
                    line_no, name, true_fields, test_fields))
            
            true_chr = true_fields[1]
            true_pos = parse_int(true_fields[2])
            aln_read_name = test_fields[0]
            if aln_read_name != true_read_name:
                raise RuntimeError('Mismatch on line {} of {} and {}.  Read names differ: {} != {}'.format(
                    line_no, true_read_stats_file, test_read_stats_file, true_read_name, aln_read_name))
            aln_chr = test_fields[1]
            aln_pos = parse_int(test_fields[2])
            # Skip over score field
            aln_mapq = parse_int(test_fields[4])
            aln_correct = 1 if aln_chr == true_chr and abs(true_pos - aln_pos) < mapeval_threshold else 0

            out.write('{}, {}, {}\n'.format(aln_read_name, aln_correct, aln_mapq))
        
        # make sure same length
        has_next = False
        try:
            iter(truth).next()
            has_next = True
        except:
            pass
        try:
            iter(test).next()
            has_next = True
        except:
            pass
        if has_next:
            raise RuntimeError('read stats files have different lengths')
        
    out_file_id = context.write_intermediate_file(job, out_file)
    return out_file_id
    
def compare_scores(job, context, baseline_name, baseline_file_id, name, score_file_id):
    """
    Compares scores from TSV files. The baseline and file under test both have
    the format:
    read name, contig aligned to, alignment position, alignment score, MAPQ
    
    Produces a CSV (NOT TSV) of the form:
    read name, score difference, aligned score, baseline score
    
    If saved to the out store it will be:
    <condition name>.compare.<baseline name>.scores
    
    Uses the given (condition) name as a file base name for the file under test.
    
    """
    work_dir = job.fileStore.getLocalTempDir()

    baseline_read_stats_file = os.path.join(work_dir, 'baseline.tsv')
    job.fileStore.readGlobalFile(baseline_file_id, baseline_read_stats_file)
    test_read_stats_file = os.path.join(work_dir, name + '.tsv')
    job.fileStore.readGlobalFile(score_file_id, test_read_stats_file)

    out_file = os.path.join(work_dir, '{}.compare.{}.scores'.format(name, baseline_name))

    with open(baseline_read_stats_file) as baseline, open(test_read_stats_file) as test, \
         open(out_file, 'w') as out:
        line_no = 0
        for baseline_fields, test_fields in itertools.izip(tsv.TsvReader(baseline), tsv.TsvReader(test)):
            # Zip everything up and assume that the reads correspond
            line_no += 1
            
            if len(baseline_fields) != 5 or len(test_fields) != 5:
                raise RuntimeError('Incorrect field counts on line {} for {}: {} and {}'.format(
                    line_no, name, baseline_fields, test_fields))
            
            if baseline_fields[0] != test_fields[0]:
                # Read names must correspond or something has gone wrong
                raise RuntimeError('Mismatch on line {} of {} and {}.  Read names differ: {} != {}'.format(
                    line_no, baseline_read_stats_file, test_read_stats_file, baseline_fields[0], test_fields[0]))
            
            # Order is: name, conting, pos, score, mapq
            aligned_score = test_fields[3]
            baseline_score = baseline_fields[3]
            # Compute the score difference. Scores are integers.
            score_diff = parse_int(aligned_score) - parse_int(baseline_score)
            
            # Report the score difference            
            out.write('{}, {}, {}, {}\n'.format(baseline_fields[0], score_diff, aligned_score, baseline_score))
        
        # make sure same length
        has_next = False
        try:
            iter(baseline).next()
            has_next = True
        except:
            pass
        try:
            iter(test).next()
            has_next = True
        except:
            pass
        if has_next:
            raise RuntimeError('read stats files have different lengths')
        
    out_file_id = context.write_intermediate_file(job, out_file)
    return out_file_id

def get_gam_scores(job, context, xg_file_id, name, gam_file_id):
    """
    extract read stats from gam, return id of scores file
    
    Read stats file is a TSV of read name, contig name, contig offset, score, mapping quality

    """

    work_dir = job.fileStore.getLocalTempDir()

    # download input
    xg_file = os.path.join(work_dir, '{}.xg'.format(name))
    job.fileStore.readGlobalFile(xg_file_id, xg_file)
    gam_file = os.path.join(work_dir, name)
    job.fileStore.readGlobalFile(gam_file_id, gam_file)

    out_pos_file = gam_file + '.tsv'
                           
    # go through intermediate json file until docker worked out
    gam_annot_json = gam_file + '.json'
    cmd = [['vg', 'annotate', '-p', '-a', os.path.basename(gam_file), '-x', os.path.basename(xg_file)]]
    cmd.append(['vg', 'view', '-aj', '-'])
    with open(gam_annot_json, 'w') as output_annot_json:
        context.runner.call(job, cmd, work_dir = work_dir, outfile=output_annot_json)

    # turn the annotated gam json into truth positions, as separate command since
    # we're going to use a different docker container.  (Note, would be nice to
    # avoid writing the json to disk)        
    jq_cmd = [['jq', '-c', '-r', '[.name, .refpos[0].name, .refpos[0].offset,'
               'if .mapping_quality == null then 0 else .mapping_quality end ] | @tsv',
               os.path.basename(gam_annot_json)]]
    jq_cmd.append(['sed', 's/null/0/g'])

    with open(out_pos_file + '.unsorted', 'w') as out_pos:
        context.runner.call(job, jq_cmd, work_dir = work_dir, outfile=out_pos)

    # get rid of that big json asap
    os.remove(gam_annot_json)

    # sort the positions file (not piping due to memory fears)
    sort_cmd = ['sort', os.path.basename(out_pos_file) + '.unsorted']
    with open(out_pos_file, 'w') as out_pos:
        context.runner.call(job, sort_cmd, work_dir = work_dir, outfile = out_pos)

    out_stats_file_id = context.write_intermediate_file(job, out_pos_file)
    return out_stats_file_id


def run_map_eval_index(job, context, xg_file_ids, gcsa_file_ids, id_range_file_ids, vg_file_ids):
    """ 
    Index the given vg files.
    
    If no vg files are provided, pass through the given indexes, which must be
    provided.
    
    Returns a list of tuples of the form (xg, (gcsa, lcp), id_ranges), holding
    file IDs for different index components.
    
    """

    # index_ids are of the form (xg, (gcsa, lcp), id_ranges ) as returned by run_indexing
    index_ids = []
    if vg_file_ids:
        for vg_file_id in vg_file_ids:
            index_ids.append(job.addChildJobFn(run_indexing, context, [vg_file_id], ['default.vg'], 'index', 'default', 
                             cores=context.config.misc_cores, memory=context.config.misc_mem,
                             disk=context.config.misc_disk).rv())
    else:
        for i, xg_id in enumerate(xg_file_ids):
            index_ids.append((xg_id, gcsa_file_ids[i] if gcsa_file_ids else None,
                              id_range_file_ids[i] if id_range_file_ids else None))

    
    return index_ids
            


def run_map_eval_align(job, context, index_ids, gam_names, gam_file_ids, reads_gam_file_id, fasta_file_id, bwa_index_ids, do_bwa, do_bwa_paired, do_vg_paired):
    """
    Run alignments, if alignment files have not already been provided.
    
    Returns a list of graph/gam names, a list of associated gam file IDs, a
    list of associated xg index IDs, and a list of BAM file IDs (or Nones) for
    realigned read BAMs.
    
    We need to modify the name and index lists because we synthesize paired-end
    versions of existing entries.
    
    """

    # scrape out the xg ids, don't need others any more after this step
    xg_ids = [index_id[0] for index_id in index_ids]

    do_vg_mapping = not gam_file_ids
    if do_vg_mapping:
        gam_file_ids = []
        # run vg map if requested
        for i, index_id in enumerate(index_ids):
            gam_file_ids.append(job.addChildJobFn(run_mapping, context, False,
                                                  'input.gam', 'aligned-{}'.format(gam_names[i]),
                                                  False, index_id[0], index_id[1],
                                                  None, [reads_gam_file_id],
                                                  cores=context.config.misc_cores,
                                                  memory=context.config.misc_mem, disk=context.config.misc_disk).rv())

    if do_vg_mapping and do_vg_paired:
        # run paired end version of all vg inputs if --pe-gams specified
        for i, index_id in enumerate(index_ids):
            gam_file_ids.append(job.addChildJobFn(run_mapping, context, False,
                                                  'input.gam', 'aligned-{}-pe'.format(gam_names[i]),
                                                  True, index_id[0], index_id[1],
                                                  None, [reads_gam_file_id],
                                                  cores=context.config.misc_cores,
                                                  memory=context.config.misc_mem, disk=context.config.misc_disk).rv())
            
        # make sure associated lists are extended to fit new paired end mappings
        for i in range(len(xg_ids)):
            xg_ids.append(xg_ids[i])
            gam_names.append(gam_names[i] + '-pe')
        
    # run bwa if requested
    bwa_bam_file_ids = [None, None]
    if do_bwa or do_bwa_paired:
        bwa_bam_file_ids = job.addChildJobFn(run_bwa_index, context,
                                             do_bwa, do_bwa_paired, reads_gam_file_id,
                                             fasta_file_id, bwa_index_ids,
                                             cores=context.config.alignment_cores, memory=context.config.alignment_mem,
                                             disk=context.config.alignment_disk).rv()
    
    return gam_names, gam_file_ids, xg_ids, bwa_bam_file_ids    
    
def run_map_eval_comparison(job, context, xg_file_ids, gam_names, gam_file_ids,
                            bam_names, bam_file_ids, pe_bam_names, pe_bam_file_ids,
                            bwa_bam_file_ids, true_read_stats_file_id, mapeval_threshold,
                            score_baseline_name=None, original_read_gam=None):
    """
    run the mapping comparison.  Dump some tables into the outstore.
    
    Returns a pair of the position comparison results and the score comparison results.
    
    The score comparison results are a dict from baseline name to comparison
    against that baseline. Each comparison's data is a tuple of a list of
    individual per-graph comparison file IDs and an overall stats file for that
    comparison.
    
    If score_baseline_name is specified, all GAMs have their scores compared
    against the scores from the GAM with that name as a baseline.
    
    If original_read_gam is specified, all GAMs have their scores compared
    against that GAM's scores as a baseline.
    
    Each result set is itself a pair, consisting of a list of per-graph file IDs, and an overall statistics file ID.
    
    """

    # munge out the returned pair from run_bwa_index()
    if bwa_bam_file_ids[0] is not None:
        bam_file_ids.append(bwa_bam_file_ids[0])
        bam_names.append('bwa-mem')
    if bwa_bam_file_ids[1] is not None:
        pe_bam_file_ids.append(bwa_bam_file_ids[1])
        pe_bam_names.append('bwa-mem-pe')

    # get the bwa read alignment statistics, one id for each bam_name
    bam_stats_file_ids = []
    for bam_i, bam_id in enumerate(bam_file_ids):
        name = '{}-{}.bam'.format(bam_names[bam_i], bam_i)
        bam_stats_file_ids.append(job.addChildJobFn(extract_bam_read_stats, context, name, bam_id, False,
                                                    cores=context.config.misc_cores, memory=context.config.misc_mem,
                                                    disk=context.config.misc_disk).rv())
    # separate flow for paired end bams because different logic used
    pe_bam_stats_file_ids = []
    for bam_i, bam_id in enumerate(pe_bam_file_ids):
        name = '{}-{}.bam'.format(pe_bam_names[bam_i], bam_i)
        pe_bam_stats_file_ids.append(job.addChildJobFn(extract_bam_read_stats, context, name, bam_id, True,
                                                       cores=context.config.misc_cores, memory=context.config.misc_mem,
                                                       disk=context.config.misc_disk).rv())

    # get the gam read alignment statistics, one for each gam_name (todo: run vg map like we do bwa?)
    gam_stats_file_ids = []
    # We also need to keep the jobs around so we can enforce that things run after them.
    gam_stats_jobs = []
    for gam_i, gam_id in enumerate(gam_file_ids):
        name = '{}-{}.gam'.format(gam_names[gam_i], gam_i)
        # run_mapping will return a list of gam_ids.  since we don't
        # specify id ranges, this will always have one entry
        gam = gam_id
        if type(gam_id) is list:
            assert len(gam_id) == 1
            gam = gam_id[0]
        RealtimeLogger.info('Work on GAM {} = {} named {} with XG id {}'.format(gam_i, gam, gam_names[gam_i], xg_file_ids[gam_i]))
        
        # Make a job to annotate the GAM
        annotate_job = job.addChildJobFn(annotate_gam, context, xg_file_ids[gam_i], gam,
                                         cores=context.config.misc_cores, memory=context.config.misc_mem,
                                         disk=context.config.misc_disk)
        
        # Then compute stats on the annotated GAM
        gam_stats_jobs.append(annotate_job.addFollowOnJobFn(extract_gam_read_stats, context,
                                                            name, annotate_job.rv(),
                                                            cores=context.config.misc_cores, memory=context.config.misc_mem,
                                                            disk=context.config.misc_disk))
        
        gam_stats_file_ids.append(gam_stats_jobs[-1].rv())
    

    # compare all our positions, and dump results to the out store. Get a tuple
    # of individual comparison files and overall stats file.
    position_comparison_results = job.addFollowOnJobFn(run_map_eval_compare_positions, context,
                                                       true_read_stats_file_id, gam_names, gam_stats_file_ids,
                                                       bam_names, bam_stats_file_ids, pe_bam_names, pe_bam_stats_file_ids,
                                                       mapeval_threshold,
                                                       cores=context.config.misc_cores, memory=context.config.misc_mem,
                                                       disk=context.config.misc_disk).rv()
    
    # This will map from baseline name to score comparison data against that
    # baseline
    score_comparisons = {}
    
    if score_baseline_name is not None:
        # We want to compare the scores from all the GAMs against a baseline
        
        # Make a dict mapping from assigned GAM name in gam_names to the stats file for that GAM's alignment
        name_to_stats_id = dict(itertools.izip(gam_names, gam_stats_file_ids))
        
        # Find the baseline scores
        baseline_stats_id = name_to_stats_id[score_baseline_name]
        
        # compare all our scores against the baseline, and dump results to the
        # out store. 
        score_comp_job = job.addChildJobFn(run_map_eval_compare_scores, context, score_baseline_name, baseline_stats_id,
                                           gam_names, gam_stats_file_ids, bam_names, bam_stats_file_ids,
                                           pe_bam_names, pe_bam_stats_file_ids, cores=context.config.misc_cores,
                                           memory=context.config.misc_mem, disk=context.config.misc_disk)
                             
        for stats_job in gam_stats_jobs:
            # Make sure we don't try and get the stats GAMs too early.
            stats_job.addFollowOn(score_comp_job)
                             
        # Get a tuple of individual comparison files and overall stats file.
        score_comparisons[score_baseline_name] = score_comp_job.rv()
        
    if original_read_gam is not None:
        # Also compare against the original GAM's scores as a baseline
        
        # First compute its stats file
        stats_job = job.addChildJobFn(extract_gam_read_stats, context,
                                      name, original_read_gam,
                                      cores=context.config.misc_cores, memory=context.config.misc_mem,
                                      disk=context.config.misc_disk)
        
        # compare all our scores against this other baseline, and dump results
        # to the out store.
        score_comp_job = stats_job.addFollowOnJobFn(run_map_eval_compare_scores, context, 'input', stats_job.rv(),
                                                    gam_names, gam_stats_file_ids, bam_names, bam_stats_file_ids,
                                                    pe_bam_names, pe_bam_stats_file_ids, cores=context.config.misc_cores,
                                                    memory=context.config.misc_mem, disk=context.config.misc_disk)
                                                    
        for stats_job in gam_stats_jobs:
            # Make sure we don't try and get the stats GAMs too early.
            stats_job.addFollowOn(score_comp_job)
                                                    
        # Save the results
        score_comparisons['input'] = score_comp_job.rv()
        
        
        
    return position_comparison_results, score_comparisons

def run_map_eval_compare_positions(job, context, true_read_stats_file_id, gam_names, gam_stats_file_ids,
                         bam_names, bam_stats_file_ids, pe_bam_names, pe_bam_stats_file_ids, mapeval_threshold):
    """
    Compare the read positions for each read across the different aligmment
    methods.
    
    Produces a bunch of individual comparison files against the truth, a
    combined "positions.results.tsv" across all aligners, and a statistics file
    "stats.tsv" in the out_store.
    
    Returns the list of comparison files, and the stats file ID.
    """

    # merge up all the output data into one list
    names = gam_names + bam_names + pe_bam_names
    stats_file_ids = gam_stats_file_ids + bam_stats_file_ids + pe_bam_stats_file_ids

    compare_ids = []
    for name, stats_file_id in zip(names, stats_file_ids):
        compare_ids.append(job.addChildJobFn(compare_positions, context, true_read_stats_file_id, name,
                                             stats_file_id, mapeval_threshold,
                                             cores=context.config.misc_cores, memory=context.config.misc_mem,
                                             disk=context.config.misc_disk).rv())

    stats_file_id = job.addFollowOnJobFn(run_process_position_comparisons, context, names, compare_ids,
                                         cores=context.config.misc_cores, memory=context.config.misc_mem,
                                         disk=context.config.misc_disk).rv()
                                         
    return compare_ids, stats_file_id

def run_process_position_comparisons(job, context, names, compare_ids):
    """
    Write some raw tables of position comparisons to the output.  Compute some
    stats for each graph.
    
    Returns the stats file's file ID.
    """

    work_dir = job.fileStore.getLocalTempDir()

    map_stats = []

    # make the position.results.tsv and position.stats.tsv
    results_file = os.path.join(work_dir, 'position.results.tsv')
    with open(results_file, 'w') as out_results:
        out_results.write('correct\tmq\taligner\n')

        def write_tsv(comp_file, a):
            """
            Read the given comparison CSV for the given condition name, and dump
            it to the combined results file.
            """
            # tack on '-se' to single endings to make more consistent ordering in r plots
            method = a + '-se' if not (a.endswith('-pe') or a.endswith('-se')) else a
            
            with open(comp_file) as comp_in:
                for line in comp_in:
                    toks = line.rstrip().split(', ')
                    # TODO: why are we quoting the aligner name here? What parses TSV and respects quotes?
                    out_results.write('{}\t{}\t"{}"\n'.format(toks[1], toks[2], method))

        for name, compare_id in itertools.izip(names, compare_ids):
            compare_file = os.path.join(work_dir, '{}.compare.positions'.format(name))
            job.fileStore.readGlobalFile(compare_id, compare_file)
            context.write_output_file(job, compare_file)
            write_tsv(compare_file, name)

            map_stats.append([job.addChildJobFn(run_acc, context, name, compare_id, cores=context.config.misc_cores,
                                                memory=context.config.misc_mem, disk=context.config.misc_disk).rv(),
                              job.addChildJobFn(run_auc, context, name, compare_id, cores=context.config.misc_cores,
                                                memory=context.config.misc_mem, disk=context.config.misc_disk).rv(),
                              job.addChildJobFn(run_qq, context, name, compare_id, cores=context.config.misc_cores,
                                                memory=context.config.misc_mem, disk=context.config.misc_disk).rv()])
            
    context.write_output_file(job, results_file)

    return job.addFollowOnJobFn(run_write_position_stats, context, names, map_stats).rv()

def run_write_position_stats(job, context, names, map_stats):
    """
    write the position comparison statistics as tsv, both to the Toil fileStore
    and to the out_store as "stats.tsv".
    
    Returns the ID of the file written.
    
    This is different than the stats TSV format used internally, for read stats.
    """

    work_dir = job.fileStore.getLocalTempDir()
    stats_file = os.path.join(work_dir, 'stats.tsv')
    with open(stats_file, 'w') as stats_out:
        stats_out.write('aligner\tcount\tacc\tauc\tqq-r\n')
        for name, stats in zip(names, map_stats):
            stats_out.write('{}\t{}\t{}\t{}\t{}\n'.format(name, stats[0][0], stats[0][1],
                                                          stats[1][0], stats[2]))

    stats_file_id = context.write_output_file(job, stats_file)
    
    return stats_file_id
    
def run_acc(job, context, name, compare_id):
    """
    Percentage of correctly aligned reads (ignore quality)
    """
    
    work_dir = job.fileStore.getLocalTempDir()

    compare_file = os.path.join(work_dir, '{}.compare.positions'.format(name))
    job.fileStore.readGlobalFile(compare_id, compare_file)
    
    total = 0
    correct = 0
    with open(compare_file) as compare_f:
        for line in compare_f:
            toks = line.split(', ')
            total += 1
            if toks[1] == '1':
                correct += 1
                
    acc = float(correct) / float(total) if total > 0 else 0
    return total, acc
    
def run_auc(job, context, name, compare_id):
    """
    AUC of roc plot
    """
    if not have_sklearn:
        return ["sklearn_not_installed"] * 2 
    
    work_dir = job.fileStore.getLocalTempDir()

    compare_file = os.path.join(work_dir, '{}.compare.positions'.format(name))
    job.fileStore.readGlobalFile(compare_id, compare_file)

    try:
        data = np.loadtxt(compare_file, dtype=np.int, delimiter =', ', usecols=(1,2)).T
        auc = roc_auc_score(data[0], data[1])
        aupr = average_precision_score(data[0], data[1])
    except:
        # will happen if file is empty
        auc, aupr = 0, 0

    return auc, aupr    

def run_qq(job, context, name, compare_id):
    """
    some measure of qq consistency 
    """
    if not have_sklearn:
        return "sklearn_not_installed"

    work_dir = job.fileStore.getLocalTempDir()

    compare_file = os.path.join(work_dir, '{}.compare.positions'.format(name))
    job.fileStore.readGlobalFile(compare_id, compare_file)

    try:
        data = np.loadtxt(compare_file, dtype=np.int, delimiter =', ', usecols=(1,2))

        # this can surley be sped up if necessary
        correct = Counter()
        total = Counter()
        for row in data:
            correct[row[1]] += row[0]
            total[row[1]] += 1

        qual_scores = []
        qual_observed = []            
        for qual, cor in correct.items():
            qual_scores.append(qual)
            p_err = max(1. - float(cor) / float(total[qual]), sys.float_info.epsilon)
            observed_score =-10. * math.log10(p_err)
            qual_observed.append(observed_score)

        # should do non-linear regression as well? 
        r2 = r2_score(qual_observed, qual_scores)
    except:
        # will happen if file is empty
        r2 = 'fail'

    return r2
    
def run_map_eval_compare_scores(job, context, baseline_name, baseline_stats_file_id, gam_names, gam_stats_file_ids,
                                bam_names, bam_stats_file_ids, pe_bam_names, pe_bam_stats_file_ids):
    """
    Compare scores in the given stats files in the lists to those in the given
    baseline stats file.
    
    Stats file format is a TSV of:
    read name, contig name, contig offset, score, mapping quality
    
    Will save the output to the outstore, as a CSV of read name and score
    difference, named <GAM/BAM name>.compare.<baseline name>.scores.
    
    Will also save a concatenated TSV file, with score difference and quoted
    aligner/condition name, as score.results.<baseline_name>.tsv
    
    Returns the list of comparison file IDs and the score results file ID.
    
    For now, just ignores BAMs because we don't pull in pysam to parse out their
    scores.
    
    """
    
    # merge up all the condition names and file IDs into synchronized lists
    # TODO: until we can extract the scores from BAMs, just process the GAMs
    names = gam_names
    stats_file_ids = gam_stats_file_ids
    
    RealtimeLogger.info(names)
    RealtimeLogger.info(stats_file_ids)

    compare_ids = []
    for name, stats_file_id in zip(names, stats_file_ids):
        compare_ids.append(job.addChildJobFn(compare_scores, context, baseline_name, baseline_stats_file_id, name, stats_file_id,
                                             cores=context.config.misc_cores, memory=context.config.misc_mem,
                                             disk=context.config.misc_disk).rv())

    stats_job = job.addFollowOnJobFn(run_process_score_comparisons, context, baseline_name, names, compare_ids,
                                     cores=context.config.misc_cores, memory=context.config.misc_mem,
                                     disk=context.config.misc_disk)
                                     
    return compare_ids, stats_job.rv()

def run_process_score_comparisons(job, context, baseline_name, names, compare_ids):
    """
    Write some raw tables of score comparisons against the given baseline to the
    output.  Compute some stats for each graph.
    
    Returns the file ID of the overall stats file "score.stats.<baseline name>.tsv".
    """

    work_dir = job.fileStore.getLocalTempDir()

    # Holds a list (by aligner) of lists (by type of statistic) of stats info
    # (that might be tuples, depending on the stat)
    # TODO: Change this to dicts by stat type.
    map_stats = []

    # make the score.results.tsv, which holds score differences and aligner/condition names.
    results_file = os.path.join(work_dir, 'score.{}.results.tsv'.format(baseline_name))
    with open(results_file, 'w') as out_results_file:
        out_results = tsv.TsvWriter(out_results_file)
        out_results.comment('diff\taligner')

        def write_tsv(comp_file, a):
            """
            Read the given comparison CSV for the given condition name, and dump
            it to the combined results file.
            """
            with open(comp_file) as comp_in:
                for line in comp_in:
                    toks = line.rstrip().split(', ')
                    out_results.line(toks[1], a)

        for name, compare_id in itertools.izip(names, compare_ids):
            compare_file = os.path.join(work_dir, '{}.compare.{}.scores'.format(name, baseline_name))
            job.fileStore.readGlobalFile(compare_id, compare_file)
            context.write_output_file(job, compare_file)
            write_tsv(compare_file, name)

            # Tabulate overall statistics
            map_stats.append([job.addChildJobFn(run_portion_worse, context, name, compare_id,
                                                cores=context.config.misc_cores, memory=context.config.misc_mem,
                                                disk=context.config.misc_disk).rv()])
            
    context.write_output_file(job, results_file)
    
    return job.addFollowOnJobFn(run_write_score_stats, context, baseline_name, names, map_stats).rv()
    
def run_write_score_stats(job, context, baseline_name, names, map_stats):
    """
    write the score comparison statistics against the baseline with the given
    name as tsv named "score.stats.<baseline name>.tsv".
    
    Returns the file ID for that file.
    
    This is different than the stats TSV format used internally, for read stats.
    """

    work_dir = job.fileStore.getLocalTempDir()
    stats_file = os.path.join(work_dir, 'score.stats.{}.tsv'.format(baseline_name))
    with open(stats_file, 'w') as stats_out_file:
        # Put each stat as a different column.
        stats_out = tsv.TsvWriter(stats_out_file)
        stats_out.comment('aligner\tcount\tworse')
        for name, stats in zip(names, map_stats):
            stats_out.line(name, stats[0][0], stats[0][1])

    return context.write_output_file(job, stats_file)
    
def run_portion_worse(job, context, name, compare_id):
    """
    Compute percentage of reads that get worse from the baseline graph.
    Return total reads and portion that got worse.
    """
    
    work_dir = job.fileStore.getLocalTempDir()

    compare_file = os.path.join(work_dir, '{}.compare.scores'.format(name))
    job.fileStore.readGlobalFile(compare_id, compare_file)
    
    total = 0
    worse = 0
    with open(compare_file) as compare_f:
        for line in compare_f:
            toks = line.split(', ')
            total += 1
            if int(toks[1]) < 0:
                worse += 1
                
    portion = float(worse) / float(total) if total > 0 else 0
    return total, portion

def run_mapeval(job, context, options, xg_file_ids, gcsa_file_ids, id_range_file_ids,
                vg_file_ids, gam_file_ids, reads_gam_file_id, fasta_file_id, bwa_index_ids, bam_file_ids,
                pe_bam_file_ids, true_read_stats_file_id):
    """
    Main Toil job, and main entrypoint for use of vg_mapeval as a library.
    
    Run the analysis on the given files.
    
    Return the file IDs of result files.
    
    Returns a pair of the position comparison results and the score comparison
    results.
    
    Each result set is itself a pair, consisting of a list of per-graph file
    IDs, and an overall statistics file ID.
    
    """
    
    # This should be the only Toil job that actually uses options (in order to
    # orchestrate the right shape of workflow depending on whether we want
    # particular analyses done).
    
    # Make an indexing job
    index_job = job.addChildJobFn(run_map_eval_index,
                                  context,
                                  xg_file_ids,
                                  gcsa_file_ids,
                                  id_range_file_ids,
                                  vg_file_ids,
                                  cores=context.config.misc_cores,
                                  memory=context.config.misc_mem,
                                  disk=context.config.misc_disk)
                              
    # Then after indexing, do alignment
    alignment_job = index_job.addFollowOnJobFn(run_map_eval_align, context, index_job.rv(),
                                               options.gam_names, gam_file_ids, reads_gam_file_id,
                                               fasta_file_id, bwa_index_ids, options.bwa,
                                               options.bwa_paired, options.vg_paired)
                                               
    # Unpack the alignment job's return values
    # TODO: we're clobbering input values...
    (gam_names, gam_file_ids, xg_ids, bwa_bam_file_ids) = (alignment_job.rv(0), 
        alignment_job.rv(1), alignment_job.rv(2), alignment_job.rv(3))

    # Then do mapping evaluation comparison (the rest of the workflow)
    comparison_job = alignment_job.addFollowOnJobFn(run_map_eval_comparison, context, xg_ids,
                     gam_names, gam_file_ids, options.bam_names, bam_file_ids,
                     options.pe_bam_names, pe_bam_file_ids, bwa_bam_file_ids,
                     true_read_stats_file_id, options.mapeval_threshold, options.compare_gam_scores, reads_gam_file_id,
                     cores=context.config.misc_cores, memory=context.config.misc_mem,
                     disk=context.config.misc_disk)
                     
    return comparison_job.rv()
    
def make_mapeval_plan(toil, options):
    """
    Import all the necessary files form options into Toil.
    
    Keep the IDs under names in an argparse namespace that functions as a "plan"
    for the workflow.
    
    """
    
    # Make a plan
    plan = argparse.Namespace()
    
    start_time = timeit.default_timer()
            
    # Upload local files to the remote IO Store
    
    # Input vg data (either pre-aligned or to align against). Can be either .vg
    # (to index and align against) or .xg/.gcsa/.gcsa.lcp (to align against) or
    # .gam/.xg (pre-alligned, just annotate)
    plan.gam_file_ids = []
    if options.gams:
        for gam in options.gams:
            plan.gam_file_ids.append(toil.importFile(gam))

    plan.vg_file_ids = []
    if options.vg_graphs:
        for graph in options.vg_graphs:
            plan.vg_file_ids.append(toil.importFile(graph))

    plan.xg_file_ids = []
    plan.gcsa_file_ids = [] # list of gcsa/lcp pairs
    plan.id_range_file_ids = []
    if options.index_bases:
        for ib in options.index_bases:
            plan.xg_file_ids.append(toil.importFile(ib + '.xg'))
            if not options.gams:
                plan.gcsa_file_ids.append(
                    (toil.importFile(ib + '.gcsa'),
                    toil.importFile(ib + '.gcsa.lcp')))
                # multiple gam outputs not currently supported by evaluation pipeline
                #if os.path.isfile(os.path.join(ib, '_id_ranges.tsv')):
                #    id_range_file_ids.append(
                #        toil.importFile(ib + '_id_ranges.tsv'))
                    
    # Import input reads to be realigned
    if options.gam_input_reads:
        plan.reads_gam_file_id = toil.importFile(options.gam_input_reads)
    else:
        plan.reads_gam_file_id = None                                           
                                
    # Input bwa data        
    plan.bam_file_ids = []
    if options.bams:
        for bam in options.bams:
            plan.bam_file_ids.append(toil.importFile(bam))
    plan.pe_bam_file_ids = []
    if options.pe_bams:
        for bam in options.pe_bams:
            plan.pe_bam_file_ids.append(toil.importFile(bam))

    if options.fasta:
        plan.bwa_index_ids = dict()
        for suf in ['.amb', '.ann', '.bwt', '.pac', '.sa']:
            fidx = '{}{}'.format(options.fasta, suf)
            if os.path.exists(fidx):
                plan.bwa_index_ids[suf] = toil.importFile(fidx)
            else:
                plan.bwa_index_ids = None
                break
        if not plan.bwa_index_ids:
            plan.fasta_file_id = toil.importFile(options.fasta)
    else:
        plan.fasta_file_id = None
        plan.bwa_index_ids = None
    plan.true_read_stats_file_id = toil.importFile(options.truth)

    end_time = timeit.default_timer()
    logger.info('Imported input files into Toil in {} seconds'.format(end_time - start_time))
    
    return plan

def mapeval_main(context, options):
    """
    Run the mapeval workflow.
    """

    # Make sure the options are good
    validate_options(options)
    
    # How long did it take to run the entire pipeline, in seconds?
    run_time_pipeline = None
        
    # Mark when we start the pipeline
    start_time_pipeline = timeit.default_timer()
    
    with context.get_toil(options.jobStore) as toil:
        if not toil.options.restart:

            # Import everything
            plan = make_mapeval_plan(toil, options)

            # Make a job to run the mapeval workflow, using all these various imported files.
            main_job = Job.wrapJobFn(run_mapeval,
                                     context, 
                                     options, 
                                     plan.xg_file_ids,
                                     plan.gcsa_file_ids, 
                                     plan.id_range_file_ids,
                                     plan.vg_file_ids, 
                                     plan.gam_file_ids, 
                                     plan.reads_gam_file_id, 
                                     plan.fasta_file_id, 
                                     plan.bwa_index_ids, 
                                     plan.bam_file_ids,
                                     plan.pe_bam_file_ids, 
                                     plan.true_read_stats_file_id)
                
            # Output files all live in the out_store, but if we wanted to we could export them also/instead.
            
            # Run the root job
            toil.start(main_job)
        else:
            toil.restart()
            
    end_time_pipeline = timeit.default_timer()
    run_time_pipeline = end_time_pipeline - start_time_pipeline
 
    print("All jobs completed successfully. Pipeline took {} seconds.".format(run_time_pipeline))
    
