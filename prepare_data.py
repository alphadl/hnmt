import collections
import itertools
import numpy as np
import random

LineLengths = collections.namedtuple('LineLengths',
    ['idx', 'src_len', 'tgt_len'])
LineStatistics = collections.namedtuple('LineStatistics',
    ['idx', 'shard', 'group', 'src_len', 'tgt_len', 'src_unks', 'tgt_unks'])

class SplitNode(object):
    def __init__(self, threshold, left, right, tgt=False):
        self.threshold = threshold
        self.left = left
        self.right = right
        self.tgt = tgt

    def decide(self, linelens):
        val = linelens.tgt_len if self.tgt else linelens.src_len
        if val < self.threshold:
            return self.left.decide(linelens)
        else:
            return self.right.decide(linelens)

    def __repr__(self):
        return 'S({}:{}, {}, {})'.format(
            'tgt' if self.tgt else 'src',
            self.threshold,
            repr(self.left),
            repr(self.right))

class LeafNode(object):
    def __init__(self, group_idx):
        self.group_idx = group_idx

    def decide(self, linelens):
        return self.group_idx

    def __repr__(self):
        return 'L({})'.format(self.group_idx)


class ShardedData(object):
    def __init__(self,
                 src_lines,
                 tgt_lines,
                 src_encoder,
                 tgt_encoder,
                 src_max_len=600,
                 tgt_max_len=600,
                 min_lines_per_group=128,
                 max_lines_per_shard=1000000,
                 min_saved_padding=2048):
        # callables, yielding tokenized lines
        self.src_lines = src_lines
        self.tgt_lines = tgt_lines
        # single new-style encoder per side
        self.src_encoder = src_encoder
        self.tgt_encoder = tgt_encoder
        self.src_max_len = src_max_len
        self.tgt_max_len = tgt_max_len
        self.min_lines_per_group = min_lines_per_group
        self.max_lines_per_shard = max_lines_per_shard
        self.min_saved_padding = min_saved_padding
        # first LineLengths
        self.line_lens = []
        # later LineStatistics
        self.line_statistics = []
        self.n_shards = None
        self.shard_indices = None
        # decision tree
        self.padding_group_thresholds = None
        self.n_groups = 0

    def prepare_data(self):
        self.collect_statistics()
        self.encode()

    def collect_statistics(self):
        # first pass
        for (i, (src, tgt)) in enumerate(safe_zip(self.src_lines(),
                                                  self.tgt_lines())):
            src_len = len(src.surface)
            tgt_len = len(tgt.surface)
            # filter out too long lines
            if src_len > self.src_max_len:
                continue
            if tgt_len > self.tgt_max_len:
                continue
            # total line count => shard sizes/num
            # length distribution => thresholds for padding groups
            self.line_lens.append(LineLengths(i, src_len, tgt_len))
            # token counts => vocabulary index (encoder)
            self.src_encoder.count(src)
            self.tgt_encoder.count(tgt)
        # preassign sentences to shards by random draw without replacement
        self.n_shards = int(np.ceil(len(self.line_lens) / self.max_lines_per_shard))
        lines_per_shard = int(np.ceil(len(self.line_lens) / self.n_shards))
        self.shard_indices = [j for i in range(self.n_shards) for j in [i] * lines_per_shard]
        random.shuffle(self.shard_indices)
        # choose thresholds for padding groups
        self.padding_group_thresholds = self.choose_thresholds(self.line_lens, tgt=False)
        # decide vocabularies for encoders
        self.src_encoder.done()
        self.tgt_encoder.done()

    def choose_thresholds(self, lines, tgt):
        if tgt:
            lenfunc = lambda x: x.tgt_len
        else:
            lenfunc = lambda x: x.src_len
        # sort lengths
        lines = sorted(lines, key=lenfunc)
        lens = np.array([lenfunc(x) for x in lines])
        # select threshold to maximize reduced padding waste
        waste = lens[-1] - lens
        savings = np.arange(len(lens)) * waste
        mid = np.argmax(savings)
        # criteria for split
        split_ok = True
        if savings[mid] < self.min_saved_padding:
            # savings are not big enough
            split_ok = False
        if min(mid, len(lens) - mid) < self.min_lines_per_group:
            # too small group
            split_ok = False
        if split_ok:
            threshold = lens[mid]
            left = self.choose_thresholds(lines[:mid], not tgt)
            right = self.choose_thresholds(lines[mid:], not tgt)
            return SplitNode(threshold, left, right, tgt)
        else:
            leaf = LeafNode(self.n_groups)
            self.n_groups += 1
            return leaf


    def encode(self):
        # second pass
        for shard in range(self.n_shards):
            lines_in_shard = {line.idx: line 
                              for (line, sid)
                              in zip(self.line_lens, self.shard_indices)
                              if sid == shard}
            encoded = [list() for _ in range(self.n_groups)]
            # one pass over the data per shard
            for (i, (src, tgt)) in enumerate(safe_zip(self.src_lines(),
                                                      self.tgt_lines())):
                stats = lines_in_shard.get(i, None)
                if stats is None:
                    # drops too long and lines belonging to other shards
                    continue
                # choose padding group by lengths
                group = self.padding_group_thresholds.decide(stats)
                # encode
                src_enc = self.src_encoder.encode(src)
                tgt_enc = self.tgt_encoder.encode(tgt)
                encoded[group].append((src_enc, tgt_enc))
                # also track number of unks
                self.line_statistics.append(
                    (line.idx, shard, group,
                     line.src_len, line.tgt_len,
                     len(src_enc.surface.unknowns),
                     len(trg_enc.surface.unknowns)))
            # pad and concatenate groups
            for (group, pairs) in enumerate(encoded):
                srcs, tgts = zip(*pairs)
                padded_src = self.src_encoder.pad_sentences(srcs)
                padded_trg = self.trg_encoder.pad_sentences(trgs)
                # FIXME save encoded data


def safe_zip(*iterables):
    iters = [iter(x) for x in iterables]
    sentinel = object()
    for (j, tpl) in enumerate(itertools.zip_longest(*iterables, fillvalue=sentinel)):
        for (i, val) in enumerate(tpl):
            if val is sentinel:
                raise ValueError('Column {} was too short. '
                    'Row {} (and later) missing.'.format(i, j))
        yield tpl