from collections import defaultdict
import os
import pickle
import re
import subprocess

from Bio import SeqIO
from Bio.Alphabet import IUPAC
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from django.conf import settings
import pysam

from genome_finish.contig_display_utils import Junction
from genome_finish.jbrowse_genome_finish import add_contig_reads_bam_track
from main.models import Dataset
from utils import make_temp_file
from utils.bam_utils import index_bam
from utils.bam_utils import sort_bam_by_coordinate
from utils.import_util import add_dataset_to_entity

ENDPOINT_MODE_DIFFERENCE_FACTOR_CUTOFF = 0.5
REVERSED_COMPLEMENTARITY_FRACTION_CUTOFF = 0.75
ENDPOINT_FRACTION = 0.8


def get_insertion_placement_positions(contig, strategy='all_reads'):

    def _get_contig_reads_using_strategy(strategy):
        if strategy == 'all_reads':
            return extract_contig_reads(contig, 'all')
        elif strategy == 'mapped_mates_of_unmapped':
            return mapped_mates_of_unmapped_reads(contig)
        else:
            raise Exception(str(strategy) + ' not a recognized strategy')

    contig_reads = _get_contig_reads_using_strategy(strategy)
    if len(contig_reads) == 0:
            return {'error_string':
                    'No clipped reads were assembled into the contig'}

    contig_reads_dataset_exists = bool(
            contig.dataset_set.filter(
                    type=Dataset.TYPE.BWA_SV_INDICANTS).count())

    if strategy == 'all_reads' and not contig_reads_dataset_exists:
        make_contig_reads_dataset(contig, contig_reads)

        # Add bam track
        add_contig_reads_bam_track(contig, Dataset.TYPE.BWA_SV_INDICANTS)

    extracted_clipped_read_dicts = extract_left_and_right_clipped_read_dicts(
            contig_reads)
    left_clipped = extracted_clipped_read_dicts['left_clipped']
    right_clipped = extracted_clipped_read_dicts['right_clipped']

    # Right clipped reads indicate left endpoint
    left_ref_endpoints = get_top_clipped_locs(right_clipped)

    # Left clipped reads indicate right endpoint
    right_ref_endpoints = get_top_clipped_locs(left_clipped)

    left_junctions = []
    for ref_endpoint, ref_count in left_ref_endpoints:
        contig_endpoint, contig_count = find_contig_endpoint(
                contig, right_clipped[ref_endpoint], 'right')

        left_junctions.append(Junction(
                ref_endpoint, ref_count, contig_endpoint, contig_count))

    right_junctions = []
    for ref_endpoint, ref_count in right_ref_endpoints:
        contig_endpoint, contig_count = find_contig_endpoint(
                contig, left_clipped[ref_endpoint], 'left')

        right_junctions.append(Junction(
                ref_endpoint, ref_count, contig_endpoint, contig_count))

    contig.metadata['left_junctions'] = left_junctions
    contig.metadata['right_junctions'] = right_junctions

    contig.metadata['potential_reference_endpoints'] = {
        'left': left_ref_endpoints,
        'right': right_ref_endpoints
    }
    contig.save()

    ref_insertion_endpoints = {}
    if are_ref_endpoints_placeable(left_ref_endpoints):
        ref_insertion_endpoints['left'] = left_ref_endpoints[0][0]
    else:
        ref_insertion_endpoints['left'] = None

    if are_ref_endpoints_placeable(right_ref_endpoints):
        ref_insertion_endpoints['right'] = right_ref_endpoints[0][0]
    else:
        ref_insertion_endpoints['right'] = None

    # Handle case of no endpoints found
    error = None
    if (not ref_insertion_endpoints['left'] and
            not ref_insertion_endpoints['right']):
        error = {'error_string': ('Could not find left or right reference ' +
                'insertion endpoints using ' + str(len(contig_reads)) +
                ' clipped reads')}
    elif not ref_insertion_endpoints['left']:
        error = {'error_string': ('Could not find left reference ' +
                'insertion endpoint using ' + str(len(contig_reads)) +
                ' clipped reads')}
    elif not ref_insertion_endpoints['right']:
        error = {'error_string': ('Could not find right reference ' +
                'insertion endpoint using ' + str(len(contig_reads)) +
                ' clipped reads')}
    elif (ref_insertion_endpoints['left'] - ref_insertion_endpoints['right'] >
            0.5 * contig.num_bases):
        error = {'error_string': ('Left insertion endpoint found too far ' +
                'before right insertion endpoint')}
    elif (ref_insertion_endpoints['right'] - ref_insertion_endpoints['left'] >
            10 * contig.num_bases):
        error = {'error_string': ('Distance between left and right ' +
                'reference insertion endpoints more than 10x contig' +
                'length')}

    if error:
        return error

    left_clipped_same_end = left_clipped[ref_insertion_endpoints['right']]
    right_clipped_same_end = right_clipped[ref_insertion_endpoints['left']]

    contig_insertion_endpoints = find_contig_insertion_endpoints(
            contig, left_clipped_same_end,
            right_clipped_same_end)

    # Propogate error upwards
    if 'error_string' in contig_insertion_endpoints:
        return contig_insertion_endpoints

    if contig_insertion_endpoints['left'] is None:
        return {'error_string': ('Could not find left contig endpoint')}

    if contig_insertion_endpoints['right'] is None:
        return {'error_string': ('Could not find right contig endpoint')}

    # Set contig metadata fields and return endpoints
    insertion_placement_positions = {
        'reference': ref_insertion_endpoints,
        'contig': contig_insertion_endpoints
    }

    contig.metadata['contig_insertion_endpoints'] = (
            insertion_placement_positions['contig']['left'],
            insertion_placement_positions['contig']['right'])
    contig.metadata['reference_insertion_endpoints'] = (
        insertion_placement_positions['reference']['left'],
        insertion_placement_positions['reference']['right'])
    contig.save()

    return insertion_placement_positions


def mapped_mates_of_unmapped_reads(contig):
    unmapped_contig_reads = extract_contig_reads(
            contig, read_category='unmapped')
    print len(unmapped_contig_reads), 'unmapped reads in contig'

    original_align = contig.experiment_sample_to_alignment.dataset_set.get(
            type=Dataset.TYPE.BWA_ALIGN).get_absolute_location()
    original_alignmentfile = pysam.AlignmentFile(original_align)
    found_mates = []
    for read in unmapped_contig_reads:
        if not read.mate_is_unmapped:
            mate = original_alignmentfile.mate(read)
            found_mates.append(mate)
    original_alignmentfile.close()

    print len(found_mates), 'mapped mates found'
    return found_mates


def extract_contig_reads(contig, read_category='all'):

    READ_CATEGORY_TO_FILENAME_DICT = {
        'without_mates': 'bwa_align.SV_indicants_no_dups.bam',
        'clipped': 'bwa_align.clipped.bam',
        'split': 'bwa_align.split.bam',
        'unmapped': 'bwa_align.unmapped.bam'
    }

    def _read_category_to_filename(read_category):
        if read_category in READ_CATEGORY_TO_FILENAME_DICT:
            return READ_CATEGORY_TO_FILENAME_DICT[read_category]
        elif read_category == 'all':

            assembly_metadata_file = os.path.join(
                    contig.metadata['assembly_dir'],
                    'metadata.txt')
            with open(assembly_metadata_file) as fh:
                assembly_metadata_obj = pickle.load(fh)
            return assembly_metadata_obj['sv_indicants_bam']
        elif read_category == 'mates_of_unmapped':
            return mapped_mates_of_unmapped_reads(contig)
        else:
            raise Exception('read category not recognized')

    extract_contig_reads_executable = os.path.join(
            settings.TOOLS_DIR,
            'velvet/extractContigReads.pl')

    assembly_dir = contig.metadata['assembly_dir']

    contig_node_number = contig.metadata['node_number']
    cmd = [extract_contig_reads_executable, str(contig_node_number),
           assembly_dir]
    cmd = ' '.join(cmd)

    contig_reads_fasta = os.path.join(
            contig.get_model_data_dir(),
            'extracted_reads.fa')
    if not os.path.exists(contig_reads_fasta):
        with open(contig_reads_fasta, 'w') as fh:
            subprocess.call(cmd, shell=True, stdout=fh)

    p1 = re.compile('>(\S+)/(\d)')
    contig_reads = defaultdict(list)
    with open(contig_reads_fasta) as fh:
        for line in fh:
            m1 = p1.match(line)
            if m1:
                read_id = m1.group(1)
                read_number = int(m1.group(2))
                contig_reads[read_id].append(read_number)

    sv_indicant_reads_path = os.path.join(
            contig.experiment_sample_to_alignment.get_model_data_dir(),
            _read_category_to_filename(read_category))

    sam_file = pysam.AlignmentFile(sv_indicant_reads_path)
    sv_indicant_reads_in_contig = []
    for read in sam_file:
        if read.is_read1:
            read_number = 1
        elif read.is_read2:
            read_number = 2
        else:
            raise Exception('Read is neither read1 nor read2')

        contig_read_numbers = contig_reads.get(read.query_name, [])
        if read_number in contig_read_numbers:
            sv_indicant_reads_in_contig.append(read)

    # HACK: Set chromosome here while sam file is open
    # so AlignmentFile.getrname(tid) can be called
    ref_id_to_count = {}
    mapped_count = 0
    for read in sv_indicant_reads_in_contig:
        if not read.is_unmapped:
            mapped_count += 1
            if read.reference_id not in ref_id_to_count:
                ref_id_to_count[read.reference_id] = 1
            else:
                ref_id_to_count[read.reference_id] += 1

    if mapped_count:
        tid_count_sorted = sorted(
                ref_id_to_count.items(), key=lambda x: x[1], reverse=True)

        mode_chrom_tid = tid_count_sorted[0][0]
        mode_chrom_percentage = (tid_count_sorted[0][1] /
                float(mapped_count))

        # Set field
        if mode_chrom_percentage > 0.8:
            contig_seqrecord_id = sam_file.getrname(mode_chrom_tid)
            contig.metadata['chromosome'] = contig_seqrecord_id
            contig.save()

    sam_file.close()
    return sv_indicant_reads_in_contig


def make_contig_reads_dataset(contig, sv_indicant_reads_in_contig):

    # Get bam filename
    extracted_reads_bam_file = os.path.join(
            contig.get_model_data_dir(),
            'sv_indicants.bam')

    bwa_align_bam = contig.experiment_sample_to_alignment.dataset_set.get(
            type=Dataset.TYPE.BWA_ALIGN).get_absolute_location()
    sam_file = pysam.AlignmentFile(bwa_align_bam)

    # Write extracted reads into bam file
    extracted_reads_alignment_file = pysam.AlignmentFile(
            extracted_reads_bam_file, "wb", template=sam_file)
    sam_file.close()

    for read in sv_indicant_reads_in_contig:
        extracted_reads_alignment_file.write(read)

    extracted_reads_alignment_file.close()

    coordinate_sorted_bam = (os.path.splitext(extracted_reads_bam_file)[0] +
            '.coordinate_sorted.bam')
    sort_bam_by_coordinate(extracted_reads_bam_file, coordinate_sorted_bam)
    index_bam(coordinate_sorted_bam)

    # Add the bam file to contig as BWA_SV_INDICANTS dataset, overwriting it
    # if it already exists
    dataset_query = contig.dataset_set.filter(
            type=Dataset.TYPE.BWA_SV_INDICANTS)
    if dataset_query.count():
        dataset_query[0].delete()

    add_dataset_to_entity(contig,
            Dataset.TYPE.BWA_SV_INDICANTS,
            Dataset.TYPE.BWA_SV_INDICANTS,
            filesystem_location=coordinate_sorted_bam)


def extract_left_and_right_clipped_read_dicts(sv_indicant_reads_in_contig,
        clipping_threshold=0):

    SOFT_CLIP = 4
    HARD_CLIP = 5
    CLIP = [SOFT_CLIP, HARD_CLIP]

    # Separate left and right clipped reads
    left_clipped = defaultdict(list)
    right_clipped = defaultdict(list)
    for read in sv_indicant_reads_in_contig:
        if read.cigartuples is not None:
            left_clipping = (read.cigartuples[0][1]
                    if read.cigartuples[0][0] in CLIP else 0)
            right_clipping = (read.cigartuples[-1][1]
                    if read.cigartuples[-1][0] in CLIP else 0)
            if max(left_clipping, right_clipping) > clipping_threshold:
                is_left_clipped = left_clipping > right_clipping
                is_right_clipped = right_clipping > left_clipping
                if is_left_clipped:
                    left_clipped[read.reference_start].append(read)
                elif is_right_clipped:
                    right_clipped[read.reference_end].append(read)

    return {
        'left_clipped': left_clipped,
        'right_clipped': right_clipped
    }


def are_ref_endpoints_placeable(endpoints):
    """endpoints is a list of tuples of the form
    (loc, clipped_read_count) sorted by decreasing clipped_key_count
    """
    first = endpoints[0][1] if len(endpoints) > 0 else 0
    second = endpoints[1][1] if len(endpoints) > 1 else 0
    if not first * (1 - ENDPOINT_MODE_DIFFERENCE_FACTOR_CUTOFF) > second:
        return False

    return True


def get_top_clipped_locs(clipped_dict):
    """clipped_dict is a dictionary with clipping locations as
    keys and a list of reads as values
    """
    # Convert the dictionary into a list of tuples of the form
    # (loc, #reads) sorted in decreasing order of #reads
    clipped_count_list = sorted(
            [(loc, len(reads)) for loc, reads in clipped_dict.items()],
            key=lambda t: t[1], reverse=True)

    # Count up the total number of reads
    total = sum(count for loc, count in clipped_count_list)

    # Return the list that comprises ENDPOINT_FRACTION of the total reads
    included = 0
    i = 0
    while included / float(total) < ENDPOINT_FRACTION:
        included += clipped_count_list[i][1]
        i += 1
    return clipped_count_list[:i]


def write_read_query_alignments_to_fastq(reads, fastq_path):
    """Writes the aligned portion of each read into a fastq
    """

    query_alignment_seqrecords = []
    for read in reads:
        query_alignment_seqrecords.append(SeqRecord(
                Seq(read.query_alignment_sequence, IUPAC.ambiguous_dna),
                letter_annotations={
                        'phred_quality': read.query_alignment_qualities},
                id=read.query_name,
                description=''))

    with open(fastq_path, 'w') as fastq_handle:
        SeqIO.write(query_alignment_seqrecords, fastq_handle, 'fastq')


def simple_align_with_bwa_mem(reads_fq, reference_fasta, output_bam_path):

    # Ensure reference fasta is indexed
    subprocess.call(' '.join([
            '%s/bwa/bwa' % settings.TOOLS_DIR,
            'index',
            reference_fasta
            ]),
            shell=True, executable=settings.BASH_PATH)

    # Align clipped query alignment fastq to contig
    align_input_args = ' '.join([
            '%s/bwa/bwa' % settings.TOOLS_DIR,
            'mem',
            reference_fasta,
            reads_fq])

    # Bwa mem calls reads clipped slightly at the end of the genome
    # as unmapped, so filter these out with -F 0x004
    # To skip saving the SAM file to disk directly, pipe output directly to
    # make a BAM file.
    align_input_args += (' | ' + settings.SAMTOOLS_BINARY +
            ' view -F 0x004 -bS -')

    # Run alignment
    with open(output_bam_path, 'w') as fh:
        subprocess.check_call(
                align_input_args, stdout=fh,
                shell=True, executable=settings.BASH_PATH)


def get_reads_with_mode_attribute(clipped_alignment_bam, get_attr_function):

    alignment_ref_clip_positions = defaultdict(list)
    sam_file = pysam.AlignmentFile(clipped_alignment_bam)
    for read in sam_file:
        alignment_ref_clip_positions[get_attr_function(read)].append(read)

    alignment_ref_clip_positions_sorted = sorted(
            alignment_ref_clip_positions.items(),
            key=lambda x: len(x[1]), reverse=True)

    highest_consensus = (len(alignment_ref_clip_positions_sorted[0][1])
            if len(alignment_ref_clip_positions_sorted) > 0 else 0)
    second_highest_consensus = (len(alignment_ref_clip_positions_sorted[1][1])
            if len(alignment_ref_clip_positions_sorted) > 1 else 0)

    if (highest_consensus - second_highest_consensus >
            (ENDPOINT_MODE_DIFFERENCE_FACTOR_CUTOFF *
                    highest_consensus)):
        endpoint = alignment_ref_clip_positions_sorted[0][0], highest_consensus
    else:
        endpoint = None, None

    return endpoint


def is_contig_reverse_complement(contig, alignment_bam):
    
    # Check if contig is reverse complement
    total_mapped_count = 0
    reversed_complementarity_count = 0
    sam_file = pysam.AlignmentFile(alignment_bam)
    for read in sam_file:
        if not read.is_unmapped:
            total_mapped_count += 1
            if read.is_reverse:
                reversed_complementarity_count += 1

    if total_mapped_count == 0:
        return {'error_string':
                'Could not find sufficient homology to reference in contig'}

    return (reversed_complementarity_count / total_mapped_count >
            REVERSED_COMPLEMENTARITY_FRACTION_CUTOFF)


def write_contig_reverse_complement(contig):

    # Only perform alignment of right_clipped to contig
    contig_fasta = contig.dataset_set.get(
            type=Dataset.TYPE.REFERENCE_GENOME_FASTA).get_absolute_location()

    rc_contig_fasta = (os.path.splitext(contig_fasta)[0] +
            '.reverse_complement.fa')
    contig_seqrecord = SeqIO.parse(contig_fasta, 'fasta').next()
    contig_seqrecord.seq = contig_seqrecord.seq.reverse_complement()
    SeqIO.write(contig_seqrecord, rc_contig_fasta, 'fasta')

    return rc_contig_fasta


def find_contig_endpoint(contig, clipped_same_end, direction):

    assert direction in ['left', 'right']

    # Write clipped query alignment sequences to fastq
    contig_dir = contig.get_model_data_dir()
    clipped_query_alignment_fq = os.path.join(
            contig_dir,
            'clipped_query_alignment_seqs.fq')
    write_read_query_alignments_to_fastq(
            clipped_same_end,
            clipped_query_alignment_fq)

    # Get BAM filename for alignment
    clipped_to_contig_bam = os.path.join(
            contig_dir,
            'clipped_to_contig.bwa_align.bam')

    # Get contig fasta
    contig_fasta = contig.dataset_set.get(
            type=Dataset.TYPE.REFERENCE_GENOME_FASTA).get_absolute_location()

    align_to = None
    is_reverse = contig.metadata.get('is_reverse', None)
    if is_reverse is None:
        simple_align_with_bwa_mem(
                clipped_query_alignment_fq, contig_fasta,
                clipped_to_contig_bam)
        is_reverse = is_contig_reverse_complement(contig,
                clipped_to_contig_bam)

        if is_reverse:
            contig.metadata['is_reverse'] = True
            contig.save()
            rc_contig_fasta = write_contig_reverse_complement(contig)
            align_to = rc_contig_fasta

    else:
        if is_reverse:
            rc_contig_fasta = (os.path.splitext(contig_fasta)[0] +
                    '.reverse_complement.fa')
            align_to = rc_contig_fasta
        else:
            align_to = contig_fasta

    if align_to:
        simple_align_with_bwa_mem(
                clipped_query_alignment_fq, align_to,
                clipped_to_contig_bam)

    # Find contig endpoints
    if direction == 'right':
        return get_reads_with_mode_attribute(
                clipped_to_contig_bam, lambda r: r.reference_end)
    else:
        return get_reads_with_mode_attribute(
                clipped_to_contig_bam, lambda r: r.reference_start)


def find_contig_insertion_endpoints(contig,
        left_clipped_same_end, right_clipped_same_end):
    """ left_clipped_same_end/right_clipped_same_end are lists of
    left and right clipped reads all with the same left/right
    alignment endpoint, corresponding to the reference insertion
    right/left endpoint
    """
    contig_ins_left_end, _ = find_contig_endpoint(contig,
            right_clipped_same_end, 'right')
    contig_ins_right_end, _ = find_contig_endpoint(contig,
            left_clipped_same_end, 'left')

    return {
        'left': contig_ins_left_end,
        'right': contig_ins_right_end
    }
