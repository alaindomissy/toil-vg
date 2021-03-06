import logging
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import filecmp
import shutil
from contextlib import closing
from unittest import TestCase, skip
from urlparse import urlparse
from uuid import uuid4
import urllib2

import os
import posixpath
from bd2k.util.iterables import concat
from toil_vg.iostore import IOStore

log = logging.getLogger(__name__)


class VGCGLTest(TestCase):
    """
    Test toil-vg to make sure it is working correctly on some fixed input data.
    """

    @classmethod
    def setUpClass(cls):
        super(VGCGLTest, cls).setUpClass()
        # FIXME: pull up into common base class
        logging.basicConfig(level=logging.INFO)

    def _ci_input_path(self, filename):
        """
        Get the URL from which an input file can be obtained.
        """
        return 'https://{}.s3.amazonaws.com/{}/{}'.format(self.bucket_name, self.folder_name, filename)
    
    def _download_input(self, filename, local_path = None):
        # Where should we put this input file?
        tgt = os.path.join(self.workdir, filename) if local_path is None else local_path
        # And where does it come from?
        url = self._ci_input_path(filename)
        print(url)
        with open(tgt, 'w') as f:
            # Download the file from the URL
            connection = urllib2.urlopen(url)
            shutil.copyfileobj(connection, f)

    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.jobStoreLocal = '{}/local-testvg-{}'.format(self.workdir, uuid4())

        # input files all in same bucket folder, which is specified (only) here:
        self.bucket_name = 'cgl-pipeline-inputs'
        self.folder_name = 'vg_cgl/toil_vg_ci'
        
        self.base_command = concat('toil-vg', 'run',
                                   '--realTimeLogging', '--logInfo', '--reads_per_chunk', '8000',
                                   '--call_chunk_size', '20000',
                                   '--gcsa_index_cores', '8', '--kmers_cores', '8',
                                   '--alignment_cores', '4',
                                   '--calling_cores', '4', '--vcfeval_cores', '4',
                                   '--vcfeval_opts', ' --ref-overlap',
                                   '--call_opts', '-E 0')
        
        # default output store
        self.outstore = 'aws:us-west-2:toilvg-jenkinstest-outstore-{}'.format(uuid4())
        self.local_outstore = os.path.join(self.workdir, 'toilvg-jenkinstest-outstore-{}'.format(uuid4()))

    def test_1_sim_small(self):
        ''' 
        This test uses simulated reads from the small dataset from vg, created as follows:
        vg construct -r test/small/x.fa -v test/small/x.vcf.gz > small.vg
        vg index -x small.xg small.vg
        vg sim -x small.xg  -l 100 -n 10000 -s 0 -e 0.001 -i 0.0001 > small_sim_reads
        # Reads converted to small_sim_reads.fq with script setting qualities to B
        (small.vcf.gz and small.fa.gz below are just x.vcf.gz and x.fa from input)
        '''
        self.sample_reads = self._ci_input_path('small_sim_reads.fq.gz')
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.baseline = self._ci_input_path('small.vcf.gz')
        self.chrom_fa = self._ci_input_path('small.fa.gz')

        self._run(self.base_command, self.jobStoreLocal, 'sample',
                  self.local_outstore,  '--fastq', self.sample_reads,
                  '--graphs', self.test_vg_graph,
                  '--chroms', 'x', '--vcfeval_baseline', self.baseline,
                  '--vcfeval_fasta', self.chrom_fa, '--vcfeval_opts', ' --squash-ploidy')
        
        self._assertOutput('sample', self.local_outstore, f1_threshold=0.95)

    def test_2_sim_small_standalone(self):
        ''' 
        Same as above, but chain standalone tools instead of toil-vg run
        '''
        self.sample_reads = self._ci_input_path('small_sim_reads.fq.gz')
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.baseline = self._ci_input_path('small.vcf.gz')
        self.chrom_fa = self._ci_input_path('small.fa.gz')
        
        self._run('toil-vg', 'index', self.jobStoreLocal, self.local_outstore,
                  '--graphs', self.test_vg_graph, '--chroms', 'x',
                   '--gcsa_index_cores', '8', '--kmers_cores', '8',
                  '--realTimeLogging', '--logInfo', '--index_name', 'small')

        self._run('toil-vg', 'map', self.jobStoreLocal, 'sample',
                  os.path.join(self.local_outstore, 'small.xg'),
                  os.path.join(self.local_outstore, 'small.gcsa'),
                  self.local_outstore,  '--fastq', self.sample_reads,
                  '--alignment_cores', '8', '--reads_per_chunk', '8000',
                  '--realTimeLogging', '--logInfo')
        
        self._run('toil-vg', 'call', self.jobStoreLocal,
                  os.path.join(self.local_outstore, 'small.xg'), 'sample',
                  self.local_outstore, '--gams', os.path.join(self.local_outstore, 'sample_default.gam'), 
                  '--chroms', 'x', '--call_chunk_size', '20000', '--calling_cores', '4',
                  '--realTimeLogging', '--logInfo')

        self._run('toil-vg', 'vcfeval', self.jobStoreLocal,
                  os.path.join(self.local_outstore, 'sample.vcf.gz'), self.baseline,
                  self.chrom_fa, self.local_outstore,
                  '--vcfeval_opts', ' --squash-ploidy',
                  '--realTimeLogging', '--logInfo')

        self._assertOutput(None, self.local_outstore, f1_threshold=0.95)

    def test_3_sim_small_mapeval(self):
        ''' 
        Same generate and align some simulated reads
        '''
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.chrom_fa = self._ci_input_path('small.fa.gz')
        
        self._run('toil-vg', 'index', self.jobStoreLocal, self.local_outstore,
                  '--graphs', self.test_vg_graph, '--chroms', 'x',
                   '--gcsa_index_cores', '8', '--kmers_cores', '8',
                  '--realTimeLogging', '--logInfo', '--index_name', 'small')

        self._run('toil-vg', 'sim', self.jobStoreLocal,
                 os.path.join(self.local_outstore, 'small.xg'), '2000',
                  self.local_outstore, '--gam', '--sim_chunks', '5', '--maxCores', '8',
                  '--sim_opts', ' -l 150 -p 500 -v 50 -e 0.05 -i 0.01', '--seed', '0')

        self._run('toil-vg', 'map', self.jobStoreLocal, 'sample',
                  os.path.join(self.local_outstore, 'small.xg'),
                  os.path.join(self.local_outstore, 'small.gcsa'),
                  self.local_outstore,
                  '--gam_input_reads', os.path.join(self.local_outstore, 'sim.gam'),
                  '--alignment_cores', '3', '--reads_per_chunk', '1000',
                  '--realTimeLogging', '--logInfo', '--interleaved')

        # check running mapeval on the gams

        self._run('toil-vg', 'mapeval', self.jobStoreLocal,
                  self.local_outstore,
                  os.path.join(self.local_outstore, 'true.pos'),
                  '--index-bases', os.path.join(self.local_outstore, 'small'),
                  '--gam_input_reads', os.path.join(self.local_outstore, 'sim.gam'),
                  '--gams', os.path.join(self.local_outstore, 'sample_default.gam'),
                  '--gam-names', 'vg', '--realTimeLogging', '--logInfo',
                  '--maxCores', '8', '--bwa', '--bwa-paired', '--fasta', self.chrom_fa)

        self._assertMapEvalOutput(self.local_outstore, 4000, ['vg', 'bwa-mem', 'bwa-mem-pe'], 0.9)

        # check running mapeval on the indexes

        os.remove(os.path.join(self.local_outstore, 'stats.tsv'))

        self._run('toil-vg', 'mapeval', self.jobStoreLocal,
                  self.local_outstore,
                  os.path.join(self.local_outstore, 'true.pos'),
                  '--index-bases', os.path.join(self.local_outstore, 'small'),
                  '--gam_input_reads', os.path.join(self.local_outstore, 'sim.gam'),
                  '--gam-names', 'vg', '--realTimeLogging', '--logInfo',
                  '--alignment_cores', '8', '--vg-paired',
                  '--maxCores', '8', '--bwa', '--fasta', self.chrom_fa)
        
        self._assertMapEvalOutput(self.local_outstore, 4000, ['vg', 'vg-pe', 'bwa-mem'], 0.8)

        # check running mapeval on the vg graph
        
        os.remove(os.path.join(self.local_outstore, 'stats.tsv'))

        self._run('toil-vg', 'mapeval', self.jobStoreLocal,
                  self.local_outstore,
                  os.path.join(self.local_outstore, 'true.pos'),
                  '--vg-graphs', self.test_vg_graph,
                  '--gam_input_reads', os.path.join(self.local_outstore, 'sim.gam'),
                  '--gam-names', 'vg', '--realTimeLogging', '--logInfo',
                  '--alignment_cores', '8',                  
                  '--maxCores', '8', '--fasta', self.chrom_fa)
        
        self._assertMapEvalOutput(self.local_outstore, 4000, ['vg'], 0.9)
        
    def test_4_BRCA1_NA12877(self):
        ''' Test sample BRCA1 output, graph construction and use, and local file processing
        '''
        self._download_input('NA12877.brca1.bam_1.fq.gz')
        self._download_input('NA12877.brca1.bam_2.fq.gz')
        self._download_input('snp1kg-brca1.vg')
        self._download_input('platinum_NA12877_BRCA1.vcf.gz.tbi')
        self._download_input('platinum_NA12877_BRCA1.vcf.gz')
        self._download_input('BRCA1.fa.gz')
        
        self.sample_reads = os.path.join(self.workdir, 'NA12877.brca1.bam_1.fq.gz')
        self.sample_reads2 = os.path.join(self.workdir, 'NA12877.brca1.bam_2.fq.gz')
        self.test_vg_graph = os.path.join(self.workdir, 'snp1kg-brca1.vg')
        self.baseline = os.path.join(self.workdir, 'platinum_NA12877_BRCA1.vcf.gz')
        self.chrom_fa = os.path.join(self.workdir, 'BRCA1.fa.gz')
        
        self._run(self.base_command, self.jobStoreLocal, 'NA12877',
                  self.local_outstore, '--fastq', self.sample_reads, self.sample_reads2, '--graphs',
                  self.test_vg_graph, '--chroms', '17',
                  '--vcf_offsets', '43044293',
                  '--vcfeval_baseline', self.baseline, '--vcfeval_fasta', self.chrom_fa)

        self._assertOutput('NA12877', self.local_outstore, f1_threshold=0.45)

    def test_5_BRCA1_BRCA2_NA12877(self):
        '''  Test pipeline on chase with two chromosomes, in this case both BRCA regions
        '''
        self._download_input('NA12877.brca1.brca2.bam.fq.gz')
        self._download_input('snp1kg-brca1.vg')
        self._download_input('snp1kg-brca2.vg')        
        self._download_input('platinum_NA12877_BRCA1_BRCA2.vcf.gz.tbi')
        self._download_input('platinum_NA12877_BRCA1_BRCA2.vcf.gz')
        self._download_input('BRCA1_BRCA2.fa.gz')
        
        self.sample_reads = os.path.join(self.workdir, 'NA12877.brca1.brca2.bam.fq.gz')
        self.test_vg_graph = os.path.join(self.workdir, 'snp1kg-brca1.vg')
        self.test_vg_graph2 = os.path.join(self.workdir, 'snp1kg-brca2.vg')        
        self.baseline = os.path.join(self.workdir, 'platinum_NA12877_BRCA1_BRCA2.vcf.gz')
        self.chrom_fa = os.path.join(self.workdir, 'BRCA1_BRCA2.fa.gz')
        
        self._run(self.base_command, self.jobStoreLocal, 'NA12877',
                  self.local_outstore, '--fastq', self.sample_reads, '--graphs',
                  self.test_vg_graph, self.test_vg_graph2, '--chroms', '17', '13',
                  '--vcf_offsets', '43044293', '32314860', '--interleaved',
                  '--single_reads_chunk', '--index_name', 'genome',
                  '--vcfeval_baseline', self.baseline, '--vcfeval_fasta', self.chrom_fa)

        self._assertOutput('NA12877', self.local_outstore, f1_threshold=0.70)

        ''' Test running vg call on one gam that contains multiple paths
        '''

        self.sample_gam = os.path.join(self.local_outstore, 'NA12877_both.gam')
        with open(os.path.join(self.local_outstore, 'NA12877_13.gam')) as gam1, \
             open(os.path.join(self.local_outstore, 'NA12877_17.gam')) as gam2, \
             open(self.sample_gam, 'w') as merged_gam:
            shutil.copyfileobj(gam1, merged_gam)
            shutil.copyfileobj(gam2, merged_gam)
             
        self.xg_index = os.path.join(self.local_outstore, 'genome.xg')        

        outstore = self.local_outstore + '.2'
        self._run('toil-vg', 'call', self.jobStoreLocal,
                  self.xg_index, 'NA12877', outstore, '--gams', self.sample_gam,
                  '--chroms', '17', '13', '--vcf_offsets', '43044293', '32314860',
                  '--call_chunk_size', '23000', '--calling_cores', '4',
                  '--realTimeLogging', '--logInfo', '--call_opts', '-E 0')

        self._run('toil-vg', 'vcfeval', self.jobStoreLocal,
                  os.path.join(outstore, 'NA12877.vcf.gz'), self.baseline,
                  self.chrom_fa, outstore,
                  '--realTimeLogging', '--logInfo',
                  '--vcfeval_opts', ' --ref-overlap')

        self._assertOutput(None, outstore, f1_threshold=0.70)
                
    def test_6_sim_small_outstore(self):
        ''' 
        This is the same as test #1, but exercises --force_outstore.
        '''
        self.sample_reads = self._ci_input_path('small_sim_reads.fq.gz')
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.baseline = self._ci_input_path('small.vcf.gz')
        self.chrom_fa = self._ci_input_path('small.fa.gz')

        self._run(self.base_command, self.jobStoreLocal, 'sample',
                  self.local_outstore,  '--fastq', self.sample_reads,
                  '--graphs', self.test_vg_graph,
                  '--chroms', 'x', '--vcfeval_baseline', self.baseline,
                  '--vcfeval_fasta', self.chrom_fa, '--vcfeval_opts', ' --squash-ploidy',
                  '--force_outstore')
        
    def _run(self, *args):
        args = list(concat(*args))
        log.info('Running %r', args)
        subprocess.check_call(args)

    def _assertOutput(self, sample_name, outstore, f1_threshold=0.90):

        # grab the f1.txt file
        local_f1 = os.path.join(self.workdir, 'f1.txt')
        io_store = IOStore.get(outstore)
        f1_path = 'vcfeval_output_f1.txt'
        if sample_name:
            f1_path = sample_name + '_' + f1_path
        io_store.read_input_file(f1_path, local_f1)

        with open(local_f1) as f1_file:
            f1_score = float(f1_file.readline().strip())
        print f1_score
        self.assertTrue(f1_score >= f1_threshold)

    def _assertMapEvalOutput(self, outstore, count, names, acc_threshold):
        with open(os.path.join(outstore, 'stats.tsv')) as stats:
            table = [line for line in stats]
        headers = set()
        for row in table[1:]:
            toks = row.split()
            self.assertTrue(len(toks) == 5)
            name, count, acc, auc, qqr = toks[0], int(toks[1]), float(toks[2]), float(toks[3]), float(toks[4])
            headers.add(name)
            self.assertTrue(count == count)
            self.assertTrue(acc > acc_threshold)
            # todo: look into this more. 
            #self.assertTrue(auc > 0.9)
            #self.assertTrue(qqur > -10)
        self.assertTrue(headers == set(names))
        
    def tearDown(self):
        shutil.rmtree(self.workdir)
        subprocess.check_call(['toil', 'clean', self.jobStoreLocal])
        
