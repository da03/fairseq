# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math

import torch
import torch_struct
from fairseq import search, utils
from fairseq.data import data_utils
from fairseq.models import FairseqIncrementalDecoder

import importlib.util
spec = importlib.util.spec_from_file_location("get_fb", "/n/home11/yuntian/genbmm/opt/hmm.py")
foo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(foo)
fbs = {}
total_num_valid = [0. for _ in range(5)]
total_num_total = [0. for _ in range(5)]

class SequenceGenerator(object):
    def __init__(
        self,
        tgt_dict,
        beam_size=1,
        max_len_a=0,
        max_len_b=200,
        min_len=1,
        normalize_scores=True,
        len_penalty=1.,
        unk_penalty=0.,
        retain_dropout=False,
        temperature=1.,
        match_source_len=False,
        no_repeat_ngram_size=0,
        search_strategy=None,
        eos=None
    ):
        """Generates translations of a given source sentence.

        Args:
            tgt_dict (~fairseq.data.Dictionary): target dictionary
            beam_size (int, optional): beam width (default: 1)
            max_len_a/b (int, optional): generate sequences of maximum length
                ax + b, where x is the source length
            min_len (int, optional): the minimum length of the generated output
                (not including end-of-sentence)
            normalize_scores (bool, optional): normalize scores by the length
                of the output (default: True)
            len_penalty (float, optional): length penalty, where <1.0 favors
                shorter, >1.0 favors longer sentences (default: 1.0)
            unk_penalty (float, optional): unknown word penalty, where <0
                produces more unks, >0 produces fewer (default: 0.0)
            retain_dropout (bool, optional): use dropout when generating
                (default: False)
            temperature (float, optional): temperature, where values
                >1.0 produce more uniform samples and values <1.0 produce
                sharper samples (default: 1.0)
            match_source_len (bool, optional): outputs should match the source
                length (default: False)
        """
        #import pdb; pdb.set_trace()
        self.pad = tgt_dict.pad()
        self.unk = tgt_dict.unk()
        self.eos = tgt_dict.eos() if eos is None else eos
        self.vocab_size = len(tgt_dict)
        self.tgt_dict = tgt_dict
        self.beam_size = beam_size
        # the max beam size is the dictionary size - 1, since we never select pad
        self.beam_size = min(beam_size, self.vocab_size - 1)
        self.max_len_a = max_len_a
        self.max_len_b = max_len_b
        self.min_len = min_len
        self.normalize_scores = normalize_scores
        self.len_penalty = len_penalty
        self.unk_penalty = unk_penalty
        self.retain_dropout = retain_dropout
        self.temperature = temperature
        self.match_source_len = match_source_len
        self.no_repeat_ngram_size = no_repeat_ngram_size

        self.beam_sizes = '9:11:13:17:0'.split(':')
        self.beam_sizes = [int(beam_size) for beam_size in self.beam_sizes]
        assert temperature > 0, '--temperature must be greater than 0'

        self.search = (
            search.BeamSearch(tgt_dict) if search_strategy is None else search_strategy
        )


    @torch.no_grad()
    def generate(self, models, sample, ngram=None, **kwargs):
        """Generate a batch of translations.

        Args:
            models (List[~fairseq.models.FairseqModel]): ensemble of models
            sample (dict): batch
            prefix_tokens (torch.LongTensor, optional): force decoder to begin
                with these tokens
            bos_token (int, optional): beginning of sentence token
                (default: self.eos)
        """
        assert len(models) == 1, len(models)
        model = EnsembleModel(models)
        return self._generate2(model, sample, ngram=ngram, **kwargs)

    @torch.no_grad()
    def _generate2(
        self,
        model,
        sample,
        prefix_tokens=None,
        bos_token=None,
        ngram=None,
        **kwargs
    ):
        assert not self.retain_dropout
        model.eval()
        # model.forward normally channels prev_output_tokens into the decoder
        # separately, but SequenceGenerator directly calls model.encoder
        encoder_input = {
            k: v for k, v in sample['net_input'].items()
            if k != 'prev_output_tokens'
        }

        src_tokens = encoder_input['src_tokens']
        src_lengths = (src_tokens.ne(self.eos) & src_tokens.ne(self.pad)).long().sum(dim=1)
        input_size = src_tokens.size()
        # batch dimension goes first followed by source lengths
        bsz = input_size[0]
        src_len = input_size[1]
        beam_size = self.beam_size

        if self.match_source_len:
            max_len = src_lengths.max().item()
        else:
            max_len = min(
                int(self.max_len_a * src_len + self.max_len_b),
                # exclude the EOS marker
                model.max_decoder_positions() - 1,
            )
        assert self.min_len <= max_len, 'min_len cannot be larger than max_len, please adjust these!'

        # compute the encoder output for each beam
        encoder_outs = model.forward_encoder(encoder_input)
        padding_idx = 1
        target_lengths = sample['target'].ne(padding_idx).long().sum(-1) # bsz
        print ('max',target_lengths.max())
        if target_lengths.max() > 100:
            target_lengths = target_lengths.clamp(max=30)
        if target_lengths.max() > 60:
            print ('warning', '*'*55)
            target_lengths = target_lengths.clamp(max=60)



        length = target_lengths.max().item()

        print ('here')
        prev_beam_sizes = []
        all_tokens = []

        beam_sizes = self.beam_sizes[:length]

        device = src_tokens.device
        new_order = torch.arange(bsz).view(-1, 1).repeat(1, length).view(-1)
        finalized = [[] for i in range(bsz)]
        #import pdb; pdb.set_trace()
        new_order = new_order.to(device).long()
        encoder_outs = model.reorder_encoder_out(encoder_outs, new_order)
        for order, beam_size in enumerate(beam_sizes):
            beam_size = self.beam_size
            if order == 0:
                # goal: unigram_probs = torch.zeros(1, length, len(vocab))
                prev_output_tokens = src_tokens.new(bsz*length, 1).fill_(0)
                position = torch.arange(length).view(1, -1).repeat(bsz, 1) + 2
                position = position.to(device).long().view(-1, 1)
                lprobs, _ = model.forward_decoder(
                    prev_output_tokens, encoder_outs, temperature=self.temperature, disable_incremental_states=False, ngram=order, is_cascade=True, position=position,
                ) # bsz, V
                unigram_probs = lprobs.view(bsz, length, -1).contiguous() # bsz, length, V
                V = unigram_probs.size(-1)
                # make sure length constraints are satisfied
                ids = torch.arange(length).view(1, -1).expand(bsz, -1).to(device)
                unigram_probs[ids.ge(target_lengths.view(-1, 1)-1)] = -float('inf')
                unigram_probs[:, :, 2][ids.ge(target_lengths.view(-1, 1)-1)] = 0
                unigram_probs[:, :, 2][ids.lt(target_lengths.view(-1, 1)-1)] = -float('inf')

                scores, mapping = torch.topk(unigram_probs, beam_size, -1) # bsz, L, K

                all_tokens.append(mapping.cpu().unsqueeze(-1))

                probs = scores[:, 0].contiguous() # bsz, K
                next_words = mapping[:, 1:] # bsz, L-1, K
                new_order = torch.arange(bsz*length).view(-1, 1).repeat(1, beam_size).view(-1)
                new_order = new_order.to(device).long()
                encoder_outs = model.reorder_encoder_out(encoder_outs, new_order)
                model.reorder_incremental_state(new_order)
                position = position.view(-1, 1).repeat(1, beam_size).view(-1, 1)
                prev_beam_sizes.append(beam_size)
            else:
                prev_output_tokens = all_tokens[-1].view(-1, order).to(device) # bsz* L* K, order
                bos_tokens = src_tokens.new(prev_output_tokens.size(0), 1).fill_(0)
                prev_output_tokens = torch.cat([bos_tokens, prev_output_tokens], -1) # bsz* L* K, order+1
                prev_position = position
                position = prev_position[:,-1:].add(1) # -1, 1
                position = torch.cat([prev_position, position], -1) # bsz* L* K, order
                lprobs, _ = model.forward_decoder(
                    prev_output_tokens, encoder_outs, temperature=self.temperature, disable_incremental_states=False, ngram=order, is_cascade=True, position=position,
                ) # bsz, V
                ngram_probs = lprobs.view(bsz, length-order+1, prev_beam_sizes[-1], -1).contiguous() # bsz, length, K, V
                ngram_probs = ngram_probs[:, :-1] # bsz, length-1, K, V
                #import pdb; pdb.set_trace()
                ids = torch.arange(length-order).view(1, -1).expand(bsz, -1).to(device)
                ngram_probs[:, :, :, 2][ids.ge(target_lengths.view(-1, 1)-1-order)] = 0
                ngram_probs[ids.ge(target_lengths.view(-1, 1)-1-order)] = -float('inf')
                ngram_probs[:, :, :, 2][ids.ge(target_lengths.view(-1, 1)-1-order)] = 0
                ngram_probs[:, 0] = ngram_probs[:, 0] + probs.unsqueeze(-1)

                next_words = next_words.unsqueeze(-2).expand(-1, -1, prev_beam_sizes[-1], -1) # bsz, L-1, K, K
                next_scores = ngram_probs.gather(-1, next_words) # bsz, L-1, K, K
                #import pdb; pdb.set_trace()
                #ngram_probs = torch.zeros(bsz, length-order, prev_beam_sizes[-1], V) # length-order, K, V
                #new_order = torch.arange(
                #ngram_probs[0, :, :] = probs.unsqueeze(-1).expand(-1, V) # accum for the first step
                #states_new = [[[None for v in range(V)] for k in range(prev_beam_sizes[-1])] for l in range(length-order)]
                #for l in range(length-order):
                #    for k in range(prev_beam_sizes[-1]):
                #        state = states[l][k]
                #        for v in range(V):
                #            w = itos[v]
                #            state_w = kenlm.State()
                #            score = m.BaseScore(state, w, state_w)
                #            if w == '<s>':
                #                score = -float('inf')
                #                state_w = None
                #            if l == length-order-1:
                #                if w != '</s>':
                #                    score = -float('inf')
                #                #else:
                #                #    import pdb; pdb.set_trace()
                #            else:
                #                if w == '</s>':
                #                    score = -float('inf')
                #            ngram_probs[l][k][v] += score
                #            states_new[l][k][v] = state_w

                #states = states_new
                #import pdb; pdb.set_trace()

                #states_new = [[[states[l][k1][next_words[l][k1][k2]] for k2 in range(]]

                # TODO: 1 we need to mask incompatible; 2 transpose
                if order > 1:
                    ##import pdb; pdb.set_trace()
                    first_node = all_tokens[-1][:, :-1, :, 1:].unsqueeze(-2).expand(-1, -1, -1, prev_beam_sizes[-1], -1) # bsz, L-3, K, 1*K, order-2
                    next_node = all_tokens[-1][:, 1:, :, :-1].unsqueeze(-3).expand(-1, -1, prev_beam_sizes[-1], -1, -1) # bsz, L-3, 1*K, K, order-2
                    boolean = first_node.ne(next_node).any(-1)
                    next_scores[boolean] = -float('inf')
                # TODO: we need to mask incompatible
                ngram_probs = next_scores # bsz, length-1, K, K
                total_num_valid[order] += ngram_probs.ne(-float('inf')).float().sum().item()
                total_num_total[order] += ngram_probs.view(-1).size(0)
                print ('order', order, 'percent', total_num_valid[order]/total_num_total[order], 'valid', total_num_valid[order], 'total', total_num_total[order])

                #import pdb; pdb.set_trace()
                if order != len(beam_sizes)-1:
                    #dist = torch_struct.LinearChainCRF(ngram_probs)
                    if ngram_probs.size(-1) not in fbs:
                        fbs[ngram_probs.size(-1)] = foo.fb_max(ngram_probs.size(-1))
                    fb = fbs[ngram_probs.size(-1)]
                    #edge_marginals = dist.marginals # 1, length-2, K, K
                    #edge_max_marginals = dist.max_marginals # 1, length-2, K, K
                    with torch.no_grad():

                        edge_max_marginals = fb(ngram_probs.transpose(0, 1).contiguous()).cpu() # bsz, length-1, K, K
                        edge_marginals = edge_max_marginals.transpose(0, 1).contiguous() # bsz, length-1, K, K
                        scores, mapping = torch.topk(edge_marginals.view(bsz, length-order, -1), beam_size, -1) # bsz, length-1, beam_size
                        # scores: 1, L-1, 500
                        mapping2 = mapping.to(device) #bsz,  length-1, K2
                        #import pdb; pdb.set_trace()
                        probs = ngram_probs[:, 0].view(bsz, -1).gather(-1, mapping2[:, 0])# bsz, K2
                        x_idx0 = mapping2 // prev_beam_sizes[-1] # bsz, length-1, K2
                        x_idx = x_idx0.unsqueeze(-1) # bsz, L-1, K2, 1
                        y_idx0 = mapping2.fmod(prev_beam_sizes[-1])
                        y_idx = y_idx0.unsqueeze(-1) # bsz, L-1, K2, 1

                        new_order = torch.arange(bsz) * (length-order+1) * prev_beam_sizes[-1]
                        new_order = new_order.to(device).view(-1, 1, 1) # bsz, 1, 1
                        tmp = torch.arange(length-order) * prev_beam_sizes[-1]
                        tmp = tmp.to(device).view(1, -1, 1) # 1, length-1, 1
                        new_order = new_order + tmp + x_idx0 # bsz, length-1, K2
                        new_order = new_order.long().view(-1)
                        position = position.index_select(0, new_order) # bsz*length-1*K2, order
                        encoder_outs = model.reorder_encoder_out(encoder_outs, new_order)
                        model.reorder_incremental_state(new_order)


                    prev_mapping = all_tokens[-1].to(device) # bsz, L, K1, order
                    x_idx = x_idx.expand(-1, -1, -1, prev_mapping.size(-1))
                    prev_mapping_x_idx = prev_mapping[:, :-1].gather(2, x_idx) # bsz, L-1, K2, order

                    prev_mapping_last_y_idx = prev_mapping[:, 1:, :, -1:].gather(2, y_idx) # bsz, L-1, K, 1
                    next_words = prev_mapping_last_y_idx[:, 1:, :, 0] # bsz, L-2, K
                    mapping2 = torch.cat([prev_mapping_x_idx, prev_mapping_last_y_idx], -1) # bsz, L-1, K2, 2
                    all_tokens.append(mapping2.cpu())
                    prev_beam_sizes.append(beam_size)
                        #y_idx = next_words[:,0,:].gather(-1, y_idx) # L-1, K

                    #TODO: states_new = [[states[l][x_idx0[l][k1].item()][prev_mapping_last_y_idx[l][k1][0].item()] for k1 in range(beam_size)] for l in range(length-order-1)] # L-2, K
                    #TODO: states = states_new
# TODO:     next_words, from y, replace print_constraints
                    #total_time += end - start
                else:
                    ngram_probs = ngram_probs.transpose(-1, -2) # bsz, l, K, K
                    #import pdb; pdb.set_trace()
                    tokens_ = all_tokens[-1]

                    torch.cuda.empty_cache()
                    ngram_probs = ngram_probs.cpu()
                    dist = torch_struct.LinearChainCRF(ngram_probs)
                    argmax = dist.argmax.transpose(-1,-2) # bsz, l, K, K
                    K = argmax.size(-1)
                    argmax = argmax.contiguous().view(bsz, argmax.size(1), -1)
                    max_ids = argmax.max(-1)[1] # bsz, l
                    max_ids_x = max_ids // K # bsz, l
                    max_ids_y = max_ids.fmod(K) # bsz, l
                    tokens = torch.cat([max_ids_x, max_ids_y[:, -1:]], 1) # bsz, l+1
                    first_token = tokens[:, 0] # bsz
# tokens_[:, 0] bsz, K, order
                    first_tokens = tokens_[:, 0].gather(1, first_token.unsqueeze(-1).unsqueeze(-1).expand(-1, -1, tokens_.size(-1))).squeeze(1) # bsz, order
# tokens_: bsz, l, K, order
                    later_token = tokens[:, 1:] # bsz, l
# tokens_[:, :, :, -1]: bsz, l, K
                    later_tokens = tokens_[:, 1:, :, -1].gather(2, later_token.unsqueeze(-1)).squeeze(-1) # bsz, l
                    tokens = torch.cat([first_tokens, later_tokens], 1) # bsz, l+order
                    for i in range(bsz):
                        hypo = {
                            'tokens': tokens[i],
                            'score': 0,
                            'attention': None,  # src_len x tgt_len
                            'alignment': None,
                            'positional_scores': torch.zeros(1),
                        }
                        finalized[i].append(hypo)

                    #for i in range(beam_size):
                    #    samples = dist.sample((1, ))
                    #    all_words, perplexity = get_sent(samples[0])
                    #    #samples = dist.topk(beam_size)
                    #    print (f'sample {i} PPL {perplexity}:', ' '.join(all_words).replace(' ', '').replace('<space>', ' '))
                    break
            #new_order = torch.arange(bsz).view(-1, 1).repeat(1, beam_size).view(-1)
            #new_order = new_order.to(src_tokens.device).long()
            #encoder_outs = model.reorder_encoder_out(encoder_outs, new_order)
        return finalized

    @torch.no_grad()
    def _generate(
        self,
        model,
        sample,
        prefix_tokens=None,
        bos_token=None,
        ngram=None,
        **kwargs
    ):
        if not self.retain_dropout:
            model.eval()

        # model.forward normally channels prev_output_tokens into the decoder
        # separately, but SequenceGenerator directly calls model.encoder
        encoder_input = {
            k: v for k, v in sample['net_input'].items()
            if k != 'prev_output_tokens'
        }

        src_tokens = encoder_input['src_tokens']
        src_lengths = (src_tokens.ne(self.eos) & src_tokens.ne(self.pad)).long().sum(dim=1)
        input_size = src_tokens.size()
        # batch dimension goes first followed by source lengths
        bsz = input_size[0]
        src_len = input_size[1]
        beam_size = self.beam_size

        if self.match_source_len:
            max_len = src_lengths.max().item()
        else:
            max_len = min(
                int(self.max_len_a * src_len + self.max_len_b),
                # exclude the EOS marker
                model.max_decoder_positions() - 1,
            )
        assert self.min_len <= max_len, 'min_len cannot be larger than max_len, please adjust these!'

        # compute the encoder output for each beam
        encoder_outs = model.forward_encoder(encoder_input)
        new_order = torch.arange(bsz).view(-1, 1).repeat(1, beam_size).view(-1)
        new_order = new_order.to(src_tokens.device).long()
        encoder_outs = model.reorder_encoder_out(encoder_outs, new_order)

        # initialize buffers
        scores = src_tokens.new(bsz * beam_size, max_len + 1).float().fill_(0)
        scores_buf = scores.clone()
        tokens = src_tokens.new(bsz * beam_size, max_len + 2).long().fill_(self.pad)
        tokens_buf = tokens.clone()
        tokens[:, 0] = self.eos if bos_token is None else bos_token
        attn, attn_buf = None, None

        # The blacklist indicates candidates that should be ignored.
        # For example, suppose we're sampling and have already finalized 2/5
        # samples. Then the blacklist would mark 2 positions as being ignored,
        # so that we only finalize the remaining 3 samples.
        blacklist = src_tokens.new_zeros(bsz, beam_size).eq(-1)  # forward and backward-compatible False mask

        # list of completed sentences
        finalized = [[] for i in range(bsz)]
        finished = [False for i in range(bsz)]
        num_remaining_sent = bsz

        # number of candidate hypos per step
        cand_size = 2 * beam_size  # 2 x beam size in case half are EOS

        # offset arrays for converting between different indexing schemes
        bbsz_offsets = (torch.arange(0, bsz) * beam_size).unsqueeze(1).type_as(tokens)
        cand_offsets = torch.arange(0, cand_size).type_as(tokens)

        # helper function for allocating buffers on the fly
        buffers = {}

        def buffer(name, type_of=tokens):  # noqa
            if name not in buffers:
                buffers[name] = type_of.new()
            return buffers[name]

        def is_finished(sent, step, unfin_idx):
            """
            Check whether we've finished generation for a given sentence, by
            comparing the worst score among finalized hypotheses to the best
            possible score among unfinalized hypotheses.
            """
            assert len(finalized[sent]) <= beam_size
            if len(finalized[sent]) == beam_size or step == max_len:
                return True
            return False

        def finalize_hypos(step, bbsz_idx, eos_scores):
            """
            Finalize the given hypotheses at this step, while keeping the total
            number of finalized hypotheses per sentence <= beam_size.

            Note: the input must be in the desired finalization order, so that
            hypotheses that appear earlier in the input are preferred to those
            that appear later.

            Args:
                step: current time step
                bbsz_idx: A vector of indices in the range [0, bsz*beam_size),
                    indicating which hypotheses to finalize
                eos_scores: A vector of the same size as bbsz_idx containing
                    scores for each hypothesis
            """
            assert bbsz_idx.numel() == eos_scores.numel()

            # clone relevant token and attention tensors
            tokens_clone = tokens.index_select(0, bbsz_idx)
            tokens_clone = tokens_clone[:, 1:step + 2]  # skip the first index, which is EOS
            assert not tokens_clone.eq(self.eos).any()
            tokens_clone[:, step] = self.eos
            attn_clone = attn.index_select(0, bbsz_idx)[:, :, 1:step+2] if attn is not None else None

            # compute scores per token position
            pos_scores = scores.index_select(0, bbsz_idx)[:, :step+1]
            pos_scores[:, step] = eos_scores
            # convert from cumulative to per-position scores
            pos_scores[:, 1:] = pos_scores[:, 1:] - pos_scores[:, :-1]

            # normalize sentence-level scores
            if self.normalize_scores:
                eos_scores /= (step + 1) ** self.len_penalty

            cum_unfin = []
            prev = 0
            for f in finished:
                if f:
                    prev += 1
                else:
                    cum_unfin.append(prev)

            sents_seen = set()
            for i, (idx, score) in enumerate(zip(bbsz_idx.tolist(), eos_scores.tolist())):
                unfin_idx = idx // beam_size
                sent = unfin_idx + cum_unfin[unfin_idx]

                sents_seen.add((sent, unfin_idx))

                if self.match_source_len and step > src_lengths[unfin_idx]:
                    score = -math.inf

                def get_hypo():

                    if attn_clone is not None:
                        # remove padding tokens from attn scores
                        hypo_attn = attn_clone[i]
                    else:
                        hypo_attn = None

                    return {
                        'tokens': tokens_clone[i],
                        'score': score,
                        'attention': hypo_attn,  # src_len x tgt_len
                        'alignment': None,
                        'positional_scores': pos_scores[i],
                    }

                if len(finalized[sent]) < beam_size:
                    finalized[sent].append(get_hypo())

            newly_finished = []
            for sent, unfin_idx in sents_seen:
                # check termination conditions for this sentence
                if not finished[sent] and is_finished(sent, step, unfin_idx):
                    finished[sent] = True
                    newly_finished.append(unfin_idx)
            return newly_finished

        reorder_state = None
        batch_idxs = None
        #import pdb; pdb.set_trace()
        for step in range(max_len + 1):  # one extra step for EOS marker
            # reorder decoder internal states based on the prev choice of beams
            if reorder_state is not None:
                if batch_idxs is not None:
                    # update beam indices to take into account removed sentences
                    corr = batch_idxs - torch.arange(batch_idxs.numel()).type_as(batch_idxs)
                    reorder_state.view(-1, beam_size).add_(corr.unsqueeze(-1) * beam_size)
                model.reorder_incremental_state(reorder_state)
                encoder_outs = model.reorder_encoder_out(encoder_outs, reorder_state)

            lprobs, avg_attn_scores = model.forward_decoder(
                tokens[:, :step + 1], encoder_outs, temperature=self.temperature, disable_incremental_states = (ngram != None), ngram=ngram,
            )
            lprobs[lprobs != lprobs] = -math.inf

            lprobs[:, self.pad] = -math.inf  # never select pad
            lprobs[:, self.unk] -= self.unk_penalty  # apply unk penalty

            # handle max length constraint
            if step >= max_len:
                lprobs[:, :self.eos] = -math.inf
                lprobs[:, self.eos + 1:] = -math.inf

            # handle prefix tokens (possibly with different lengths)
            if prefix_tokens is not None and step < prefix_tokens.size(1) and step < max_len:
                prefix_toks = prefix_tokens[:, step].unsqueeze(-1).repeat(1, beam_size).view(-1)
                prefix_lprobs = lprobs.gather(-1, prefix_toks.unsqueeze(-1))
                prefix_mask = prefix_toks.ne(self.pad)
                lprobs[prefix_mask] = -math.inf
                lprobs[prefix_mask] = lprobs[prefix_mask].scatter_(
                    -1, prefix_toks[prefix_mask].unsqueeze(-1), prefix_lprobs[prefix_mask]
                )
                # if prefix includes eos, then we should make sure tokens and
                # scores are the same across all beams
                eos_mask = prefix_toks.eq(self.eos)
                if eos_mask.any():
                    # validate that the first beam matches the prefix
                    first_beam = tokens[eos_mask].view(-1, beam_size, tokens.size(-1))[:, 0, 1:step + 1]
                    eos_mask_batch_dim = eos_mask.view(-1, beam_size)[:, 0]
                    target_prefix = prefix_tokens[eos_mask_batch_dim][:, :step]
                    assert (first_beam == target_prefix).all()

                    def replicate_first_beam(tensor, mask):
                        tensor = tensor.view(-1, beam_size, tensor.size(-1))
                        tensor[mask] = tensor[mask][:, :1, :]
                        return tensor.view(-1, tensor.size(-1))

                    # copy tokens, scores and lprobs from the first beam to all beams
                    tokens = replicate_first_beam(tokens, eos_mask_batch_dim)
                    scores = replicate_first_beam(scores, eos_mask_batch_dim)
                    lprobs = replicate_first_beam(lprobs, eos_mask_batch_dim)
            elif step < self.min_len:
                # minimum length constraint (does not apply if using prefix_tokens)
                lprobs[:, self.eos] = -math.inf

            if self.no_repeat_ngram_size > 0:
                # for each beam and batch sentence, generate a list of previous ngrams
                gen_ngrams = [{} for bbsz_idx in range(bsz * beam_size)]
                cpu_tokens = tokens.cpu()
                for bbsz_idx in range(bsz * beam_size):
                    gen_tokens = cpu_tokens[bbsz_idx].tolist()
                    for ngram in zip(*[gen_tokens[i:] for i in range(self.no_repeat_ngram_size)]):
                        if ngram[-1] != self.pad:
                            gen_ngrams[bbsz_idx][tuple(ngram[:-1])] = \
                                    gen_ngrams[bbsz_idx].get(tuple(ngram[:-1]), []) + [ngram[-1]]

            # Record attention scores
            if type(avg_attn_scores) is list:
                avg_attn_scores = avg_attn_scores[0]
            if avg_attn_scores is not None:
                if attn is None:
                    attn = scores.new(bsz * beam_size, avg_attn_scores.size(1), max_len + 2)
                    attn_buf = attn.clone()
                attn[:, :, step + 1].copy_(avg_attn_scores)

            scores = scores.type_as(lprobs)
            scores_buf = scores_buf.type_as(lprobs)
            eos_bbsz_idx = buffer('eos_bbsz_idx')
            eos_scores = buffer('eos_scores', type_of=scores)

            self.search.set_src_lengths(src_lengths)

            if self.no_repeat_ngram_size > 0:
                def calculate_banned_tokens(bbsz_idx):
                    # before decoding the next token, prevent decoding of ngrams that have already appeared
                    ngram_index = tuple(cpu_tokens[bbsz_idx, step + 2 - self.no_repeat_ngram_size:step + 1].tolist())
                    banned_tokens_per_sample = gen_ngrams[bbsz_idx].get(ngram_index, [])
                    banned_tokens_per_sample = [(bbsz_idx, t) for t in banned_tokens_per_sample]
                    return banned_tokens_per_sample

                banned_tokens = []
                if step + 2 - self.no_repeat_ngram_size >= 0:
                    # no banned tokens if we haven't generated no_repeat_ngram_size tokens yet
                    for bbsz_idx in range(bsz * beam_size):
                        banned_tokens.extend(calculate_banned_tokens(bbsz_idx))

                if banned_tokens:
                    banned_tokens = torch.LongTensor(banned_tokens)
                    lprobs.index_put_(tuple(banned_tokens.t()), lprobs.new_tensor([-math.inf] * len(banned_tokens)))

            cand_scores, cand_indices, cand_beams = self.search.step(
                step,
                lprobs.view(bsz, -1, self.vocab_size),
                scores.view(bsz, beam_size, -1)[:, :, :step],
            )

            # cand_bbsz_idx contains beam indices for the top candidate
            # hypotheses, with a range of values: [0, bsz*beam_size),
            # and dimensions: [bsz, cand_size]
            cand_bbsz_idx = cand_beams.add(bbsz_offsets)

            # finalize hypotheses that end in eos, except for blacklisted ones
            # or candidates with a score of -inf
            eos_mask = cand_indices.eq(self.eos) & cand_scores.ne(-math.inf)
            eos_mask[:, :beam_size][blacklist] = 0

            # only consider eos when it's among the top beam_size indices
            torch.masked_select(
                cand_bbsz_idx[:, :beam_size],
                mask=eos_mask[:, :beam_size],
                out=eos_bbsz_idx,
            )

            finalized_sents = set()
            if eos_bbsz_idx.numel() > 0:
                torch.masked_select(
                    cand_scores[:, :beam_size],
                    mask=eos_mask[:, :beam_size],
                    out=eos_scores,
                )
                finalized_sents = finalize_hypos(step, eos_bbsz_idx, eos_scores)
                num_remaining_sent -= len(finalized_sents)

            assert num_remaining_sent >= 0
            if num_remaining_sent == 0:
                break
            assert step <= max_len

            if len(finalized_sents) > 0:
                new_bsz = bsz - len(finalized_sents)

                # construct batch_idxs which holds indices of batches to keep for the next pass
                batch_mask = cand_indices.new_ones(bsz)
                batch_mask[cand_indices.new(finalized_sents)] = 0
                batch_idxs = batch_mask.nonzero().squeeze(-1)

                eos_mask = eos_mask[batch_idxs]
                cand_beams = cand_beams[batch_idxs]
                bbsz_offsets.resize_(new_bsz, 1)
                cand_bbsz_idx = cand_beams.add(bbsz_offsets)
                cand_scores = cand_scores[batch_idxs]
                cand_indices = cand_indices[batch_idxs]
                if prefix_tokens is not None:
                    prefix_tokens = prefix_tokens[batch_idxs]
                src_lengths = src_lengths[batch_idxs]
                blacklist = blacklist[batch_idxs]

                scores = scores.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                scores_buf.resize_as_(scores)
                tokens = tokens.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                tokens_buf.resize_as_(tokens)
                if attn is not None:
                    attn = attn.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, attn.size(1), -1)
                    attn_buf.resize_as_(attn)
                bsz = new_bsz
            else:
                batch_idxs = None

            # Set active_mask so that values > cand_size indicate eos or
            # blacklisted hypos and values < cand_size indicate candidate
            # active hypos. After this, the min values per row are the top
            # candidate active hypos.
            active_mask = buffer('active_mask')
            eos_mask[:, :beam_size] |= blacklist
            torch.add(
                eos_mask.type_as(cand_offsets) * cand_size,
                cand_offsets[:eos_mask.size(1)],
                out=active_mask,
            )

            # get the top beam_size active hypotheses, which are just the hypos
            # with the smallest values in active_mask
            active_hypos, new_blacklist = buffer('active_hypos'), buffer('new_blacklist')
            torch.topk(
                active_mask, k=beam_size, dim=1, largest=False,
                out=(new_blacklist, active_hypos)
            )

            # update blacklist to ignore any finalized hypos
            blacklist = new_blacklist.ge(cand_size)[:, :beam_size]
            assert (~blacklist).any(dim=1).all()

            active_bbsz_idx = buffer('active_bbsz_idx')
            torch.gather(
                cand_bbsz_idx, dim=1, index=active_hypos,
                out=active_bbsz_idx,
            )
            active_scores = torch.gather(
                cand_scores, dim=1, index=active_hypos,
                out=scores[:, step].view(bsz, beam_size),
            )

            active_bbsz_idx = active_bbsz_idx.view(-1)
            active_scores = active_scores.view(-1)

            # copy tokens and scores for active hypotheses
            torch.index_select(
                tokens[:, :step + 1], dim=0, index=active_bbsz_idx,
                out=tokens_buf[:, :step + 1],
            )
            torch.gather(
                cand_indices, dim=1, index=active_hypos,
                out=tokens_buf.view(bsz, beam_size, -1)[:, :, step + 1],
            )
            if step > 0:
                torch.index_select(
                    scores[:, :step], dim=0, index=active_bbsz_idx,
                    out=scores_buf[:, :step],
                )
            torch.gather(
                cand_scores, dim=1, index=active_hypos,
                out=scores_buf.view(bsz, beam_size, -1)[:, :, step],
            )

            # copy attention for active hypotheses
            if attn is not None:
                torch.index_select(
                    attn[:, :, :step + 2], dim=0, index=active_bbsz_idx,
                    out=attn_buf[:, :, :step + 2],
                )

            # swap buffers
            tokens, tokens_buf = tokens_buf, tokens
            scores, scores_buf = scores_buf, scores
            if attn is not None:
                attn, attn_buf = attn_buf, attn

            # reorder incremental state in decoder
            reorder_state = active_bbsz_idx

        # sort by score descending
        for sent in range(len(finalized)):
            finalized[sent] = sorted(finalized[sent], key=lambda r: r['score'], reverse=True)
        return finalized


class EnsembleModel(torch.nn.Module):
    """A wrapper around an ensemble of models."""

    def __init__(self, models):
        super().__init__()
        self.models = torch.nn.ModuleList(models)
        self.incremental_states = None
        if all(hasattr(m, 'decoder') and isinstance(m.decoder, FairseqIncrementalDecoder) for m in models):
            self.incremental_states = {m: {} for m in models}

    def has_encoder(self):
        return hasattr(self.models[0], 'encoder')

    def max_decoder_positions(self):
        return min(m.max_decoder_positions() for m in self.models)

    @torch.no_grad()
    def forward_encoder(self, encoder_input):
        if not self.has_encoder():
            return None
        return [model.encoder(**encoder_input) for model in self.models]

    @torch.no_grad()
    def forward_decoder(self, tokens, encoder_outs, temperature=1., disable_incremental_states=False, ngram=None, is_cascade=False, position=None):
        if len(self.models) == 1:
            return self._decode_one(
                tokens,
                self.models[0],
                encoder_outs[0] if self.has_encoder() else None,
                self.incremental_states,
                log_probs=True,
                temperature=temperature,
                disable_incremental_states=disable_incremental_states,
                ngram=ngram,
                is_cascade=is_cascade,
                position=position,
            )
        assert False, len(self.models)

        log_probs = []
        avg_attn = None
        for model, encoder_out in zip(self.models, encoder_outs):
            probs, attn = self._decode_one(
                tokens,
                model,
                encoder_out,
                self.incremental_states,
                log_probs=True,
                temperature=temperature,
            )
            log_probs.append(probs)
            if attn is not None:
                if avg_attn is None:
                    avg_attn = attn
                else:
                    avg_attn.add_(attn)
        avg_probs = torch.logsumexp(torch.stack(log_probs, dim=0), dim=0) - math.log(len(self.models))
        if avg_attn is not None:
            avg_attn.div_(len(self.models))
        return avg_probs, avg_attn

    def _decode_one(
        self, tokens, model, encoder_out, incremental_states, log_probs,
        temperature=1.,
        disable_incremental_states=False,
        ngram=None,
        is_cascade=False,
        position=None,
    ):
        if disable_incremental_states:
            self.incremental_states = None
        if self.incremental_states is not None:
            decoder_out = list(model.forward_decoder(
                tokens, encoder_out=encoder_out, incremental_state=self.incremental_states[model], ngram=ngram, is_translate=True, is_cascade=is_cascade, position=position,
            ))
            #assert False, 'here'
        else:
            assert position is None, position.size()
            decoder_out = list(model.forward_decoder(tokens, encoder_out=encoder_out, ngram=ngram, is_translate=True, is_cascade=is_cascade))
        if not is_cascade:
            decoder_out[0] = decoder_out[0][:, -1:, :]
        if temperature != 1.:
            decoder_out[0].div_(temperature)
        attn = decoder_out[1] if len(decoder_out) > 1 else None
        if type(attn) is dict:
            attn = attn.get('attn', None)
        if type(attn) is list:
            attn = attn[0]
        if attn is not None:
            attn = attn[:, -1, :]
        probs = model.get_normalized_probs(decoder_out, log_probs=log_probs)
        if not is_cascade:
            probs = probs[:, -1, :]
        return probs, attn

    def reorder_encoder_out(self, encoder_outs, new_order):
        if not self.has_encoder():
            return
        return [
            model.encoder.reorder_encoder_out(encoder_out, new_order)
            for model, encoder_out in zip(self.models, encoder_outs)
        ]

    def reorder_incremental_state(self, new_order):
        if self.incremental_states is None:
            return
        for model in self.models:
            model.decoder.reorder_incremental_state(self.incremental_states[model], new_order)


class SequenceGeneratorWithAlignment(SequenceGenerator):

    def __init__(self, tgt_dict, left_pad_target=False, **kwargs):
        """Generates translations of a given source sentence.

        Produces alignments following "Jointly Learning to Align and
        Translate with Transformer Models" (Garg et al., EMNLP 2019).

        Args:
            left_pad_target (bool, optional): Whether or not the
                hypothesis should be left padded or not when they are
                teacher forced for generating alignments.
        """
        super().__init__(tgt_dict, **kwargs)
        self.left_pad_target = left_pad_target

    @torch.no_grad()
    def generate(self, models, sample, **kwargs):
        model = EnsembleModelWithAlignment(models)
        finalized = super()._generate(model, sample, **kwargs)

        src_tokens = sample['net_input']['src_tokens']
        bsz = src_tokens.shape[0]
        beam_size = self.beam_size
        src_tokens, src_lengths, prev_output_tokens, tgt_tokens = \
            self._prepare_batch_for_alignment(sample, finalized)
        if any(getattr(m, 'full_context_alignment', False) for m in model.models):
            attn = model.forward_align(src_tokens, src_lengths, prev_output_tokens)
        else:
            attn = [
                finalized[i // beam_size][i % beam_size]['attention'].transpose(1, 0)
                for i in range(bsz * beam_size)
            ]

        # Process the attn matrix to extract hard alignments.
        for i in range(bsz * beam_size):
            alignment = utils.extract_hard_alignment(attn[i], src_tokens[i], tgt_tokens[i], self.pad, self.eos)
            finalized[i // beam_size][i % beam_size]['alignment'] = alignment
        return finalized

    def _prepare_batch_for_alignment(self, sample, hypothesis):
        src_tokens = sample['net_input']['src_tokens']
        bsz = src_tokens.shape[0]
        src_tokens = src_tokens[:, None, :].expand(-1, self.beam_size, -1).contiguous().view(bsz * self.beam_size, -1)
        src_lengths = sample['net_input']['src_lengths']
        src_lengths = src_lengths[:, None].expand(-1, self.beam_size).contiguous().view(bsz * self.beam_size)
        prev_output_tokens = data_utils.collate_tokens(
            [beam['tokens'] for example in hypothesis for beam in example],
            self.pad, self.eos, self.left_pad_target, move_eos_to_beginning=True,
        )
        tgt_tokens = data_utils.collate_tokens(
            [beam['tokens'] for example in hypothesis for beam in example],
            self.pad, self.eos, self.left_pad_target, move_eos_to_beginning=False,
        )
        return src_tokens, src_lengths, prev_output_tokens, tgt_tokens


class EnsembleModelWithAlignment(EnsembleModel):
    """A wrapper around an ensemble of models."""

    def __init__(self, models):
        super().__init__(models)

    def forward_align(self, src_tokens, src_lengths, prev_output_tokens):
        avg_attn = None
        for model in self.models:
            decoder_out = model(src_tokens, src_lengths, prev_output_tokens)
            attn = decoder_out[1]['attn']
            if avg_attn is None:
                avg_attn = attn
            else:
                avg_attn.add_(attn)
        if len(self.models) > 1:
            avg_attn.div_(len(self.models))
        return avg_attn

    def _decode_one(
        self, tokens, model, encoder_out, incremental_states, log_probs,
        temperature=1.,
    ):
        if self.incremental_states is not None:
            decoder_out = list(model.forward_decoder(
                tokens,
                encoder_out=encoder_out,
                incremental_state=self.incremental_states[model],
            ))
        else:
            decoder_out = list(model.forward_decoder(tokens, encoder_out=encoder_out))
        decoder_out[0] = decoder_out[0][:, -1:, :]
        if temperature != 1.:
            decoder_out[0].div_(temperature)
        attn = decoder_out[1] if len(decoder_out) > 1 else None
        if type(attn) is dict:
            attn = attn.get('attn', None)
        if type(attn) is list:
            attn = attn[0]
        if attn is not None:
            attn = attn[:, -1, :]
        probs = model.get_normalized_probs(decoder_out, log_probs=log_probs)
        probs = probs[:, -1, :]
        return probs, attn
