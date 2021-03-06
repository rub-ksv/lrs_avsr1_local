# Copyright 2019 Shigeki Karita
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)
from argparse import Namespace
from distutils.util import strtobool
import random
import logging
import torch.nn.functional as F
import math
import numpy as np

import torch
import os
from espnet.nets.asr_interface import ASRInterface
from espnet.finetuneav.ctc import CTC
from espnet.nets.pytorch_backend.e2e_asr import CTC_LOSS_THRESHOLD
from espnet.nets.pytorch_backend.e2e_asr import Reporter
from espnet.nets.pytorch_backend.nets_utils import make_pad_mask
from espnet.finetuneav.nets_utils import th_accuracy
from espnet.nets.pytorch_backend.transformer.attention import MultiHeadedAttention
from espnet.finetuneav.attention import MultiHeadedAttention as transfMultiHeadedAttention
from espnet.finetuneav.weighttransfn import transformerNet
from espnet.finetuneav.decoder import Decoder
from espnet.nets.pytorch_backend.transformer.encoder import Encoder
from espnet.finetuneav.videoencoder import Encoder as vEncoder
from espnet.finetuneav.rmencoder import Encoder as rmEncoder
from espnet.finetuneav.ctcencoder import Encoder as ctcEncoder
from espnet.nets.pytorch_backend.transformer.initializer import initialize
from espnet.finetuneav.label_smoothing_loss import LabelSmoothingLoss
from espnet.nets.pytorch_backend.transformer.mask import subsequent_mask
from espnet.finetuneav.plot import PlotAttentionReport
from espnet.nets.scorers.ctc import CTCPrefixScorer
from espnet.finetuneav.ctcattweights import cal_weights

class E2E(ASRInterface, torch.nn.Module):
    @staticmethod
    def add_arguments(parser):
        group = parser.add_argument_group("transformer model setting")

        group.add_argument("--transformer-init", type=str, default="pytorch",
                           choices=["pytorch", "xavier_uniform", "xavier_normal",
                                    "kaiming_uniform", "kaiming_normal"],
                           help='how to initialize transformer parameters')
        group.add_argument("--transformer-input-layer", type=str, default="conv2d",
                           choices=["conv2d", "linear", "embed"],
                           help='transformer input layer type')
        group.add_argument('--transformer-attn-dropout-rate', default=None, type=float,
                           help='dropout in transformer attention. use --dropout-rate if None is set')
        group.add_argument('--transformer-lr', default=10.0, type=float,
                           help='Initial value of learning rate')
        group.add_argument('--transformer-warmup-steps', default=25000, type=int,
                           help='optimizer warmup steps')
        group.add_argument('--transformer-length-normalized-loss', default=True, type=strtobool,
                           help='normalize loss by length')

        group.add_argument('--dropout-rate', default=0.0, type=float,
                           help='Dropout rate for the encoder')
        # Encoder
        group.add_argument('--elayers', default=4, type=int,
                           help='Number of encoder layers (for shared recognition part in multi-speaker asr mode)')
        group.add_argument('--eunits', '-u', default=300, type=int,
                           help='Number of encoder hidden units')
        # Attention
        group.add_argument('--adim', default=320, type=int,
                           help='Number of attention transformation dimensions')
        group.add_argument('--aheads', default=4, type=int,
                           help='Number of heads for multi head attention')
        # Decoder
        group.add_argument('--dlayers', default=1, type=int,
                           help='Number of decoder layers')
        group.add_argument('--dunits', default=320, type=int,
                           help='Number of decoder hidden units')
        return parser

    @property
    def attention_plot_class(self):
        return PlotAttentionReport

    def __init__(self, aidim, vidim, odim, args, ignore_id=-1):
        torch.nn.Module.__init__(self)
        armidim = 11
        vrmidim = 7
        if args.transformer_attn_dropout_rate is None:
            args.transformer_attn_dropout_rate = args.dropout_rate
        self.aencoder = Encoder(
            idim=aidim,
            attention_dim=args.adim,
            attention_heads=args.aheads,
            linear_units=args.eunits,
            num_blocks=args.elayers,
            input_layer=args.transformer_input_layer,
            dropout_rate=args.dropout_rate,
            positional_dropout_rate=args.dropout_rate,
            attention_dropout_rate=args.transformer_attn_dropout_rate
        )
        self.vencoder = vEncoder(
            idim=256,
            pretrained_video_extractor=args.pretrain_video_model,
            attention_dim=args.adim,
            attention_heads=args.aheads,
            linear_units=args.eunits,
            num_blocks=args.elayers,
            input_layer=args.transformer_input_layer,
            dropout_rate=args.dropout_rate,
            positional_dropout_rate=args.dropout_rate,
            attention_dropout_rate=args.transformer_attn_dropout_rate
        )
        self.armencoder = rmEncoder(
            idim=armidim,
            attention_dim=args.adim,
            attention_heads=args.aheads,
            linear_units=args.eunits,
            num_blocks=args.elayers,
            input_layer=args.transformer_input_layer,
            dropout_rate=args.dropout_rate,
            positional_dropout_rate=args.dropout_rate,
            attention_dropout_rate=args.transformer_attn_dropout_rate
        )
        self.vrmencoder = rmEncoder(
            idim=vrmidim,
            attention_dim=args.adim,
            attention_heads=args.aheads,
            linear_units=args.eunits,
            num_blocks=args.elayers,
            input_layer=args.transformer_input_layer,
            dropout_rate=args.dropout_rate,
            positional_dropout_rate=args.dropout_rate,
            attention_dropout_rate=args.transformer_attn_dropout_rate
        )
        self.adecoder = Decoder(
            odim=odim,
            attention_dim=args.adim,
            attention_heads=args.aheads,
            linear_units=args.dunits,
            num_blocks=args.dlayers,
            dropout_rate=args.dropout_rate,
            positional_dropout_rate=args.dropout_rate,
            self_attention_dropout_rate=args.transformer_attn_dropout_rate,
            src_attention_dropout_rate=args.transformer_attn_dropout_rate
        )
        self.vdecoder = Decoder(
            odim=odim,
            attention_dim=args.adim,
            attention_heads=args.aheads,
            linear_units=args.dunits,
            num_blocks=args.dlayers,
            dropout_rate=args.dropout_rate,
            positional_dropout_rate=args.dropout_rate,
            self_attention_dropout_rate=args.transformer_attn_dropout_rate,
            src_attention_dropout_rate=args.transformer_attn_dropout_rate
        )
        self.transformerweightnet = transformerNet(odim * 2 + args.adim * 2, odim)
        #self.actcweightnet = Net(args.adim)
        #self.vctcweightnet = Net(args.adim)
        self.actcencoder = ctcEncoder(
            idim=args.adim,
            attention_dim=args.adim,
            attention_heads=args.aheads,
            linear_units=args.eunits,
            num_blocks=6,
            dropout_rate=args.dropout_rate,
            positional_dropout_rate=args.dropout_rate,
            attention_dropout_rate=args.transformer_attn_dropout_rate
        )
        self.vctcencoder = ctcEncoder(
            idim=args.adim,
            attention_dim=args.adim,
            attention_heads=args.aheads,
            linear_units=args.eunits,
            num_blocks=6,
            dropout_rate=args.dropout_rate,
            positional_dropout_rate=args.dropout_rate,
            attention_dropout_rate=args.transformer_attn_dropout_rate
        )

        self.weight_outlayer = torch.nn.Linear(4, 2)
        self.sos = odim - 1
        self.eos = odim - 1
        self.odim = odim
        self.ignore_id = ignore_id
        self.subsample = [1]
        self.reporter = Reporter()

        # self.lsm_weight = a
        self.criterion = LabelSmoothingLoss(self.odim, self.ignore_id, args.lsm_weight,
                                            args.transformer_length_normalized_loss)
        self.softmax = torch.nn.Softmax(dim=-1)
        # self.verbose = args.verbose
        self.reset_parameters(args)
        self.adim = args.adim
        self.mtlalpha = args.mtlalpha
        if args.mtlalpha > 0.0:
            self.ctc = CTC(odim, args.adim, args.dropout_rate, ctc_type=args.ctc_type, reduce=True)
        else:
            self.ctc = None

        if args.report_cer or args.report_wer:
            from espnet.nets.e2e_asr_common import ErrorCalculator
            self.error_calculator = ErrorCalculator(args.char_list,
                                                    args.sym_space, args.sym_blank,
                                                    args.report_cer, args.report_wer)
        else:
            self.error_calculator = None
        self.rnnlm = None

        

    def reset_parameters(self, args):
        # initialize parameters
        initialize(self, args.transformer_init)

    def add_sos_eos(self, ys_pad):
        from espnet.nets.pytorch_backend.nets_utils import pad_list
        eos = ys_pad.new([self.eos])
        sos = ys_pad.new([self.sos])
        ys = [y[y != self.ignore_id] for y in ys_pad]  # parse padded ys
        ys_in = [torch.cat([sos, y], dim=0) for y in ys]
        ys_out = [torch.cat([y, eos], dim=0) for y in ys]
        return pad_list(ys_in, self.eos), pad_list(ys_out, self.ignore_id)

    def target_mask(self, ys_in_pad):
        ys_mask = ys_in_pad != self.ignore_id
        m = subsequent_mask(ys_mask.size(-1), device=ys_mask.device).unsqueeze(0)
        return ys_mask.unsqueeze(-2) & m


    def forward(self, axs_pad, vxs_pad, rms_pad, ilens, ys_pad):
        '''E2E forward

        :param torch.Tensor xs_pad: batch of padded source sequences (B, Tmax, idim)
        :param torch.Tensor ilens: batch of lengths of source sequences (B)
        :param torch.Tensor ys_pad: batch of padded target sequences (B, Lmax)
        :return: ctc loass value
        :rtype: torch.Tensor
        :return: attention loss value
        :rtype: torch.Tensor
        :return: accuracy in attention decoder
        :rtype: float
        '''
        # 1. forward aencoder
        axs_pad = axs_pad[:, :max(ilens)]  # for data parallel
        asrc_mask = (~make_pad_mask(ilens.tolist())).to(axs_pad.device).unsqueeze(-2)
        ahs_pad, ahs_mask = self.aencoder(axs_pad, asrc_mask)
        self.ahs_pad = ahs_pad

        # 1. forward vencoder
        audio_length = axs_pad.size()[1]
        vxs_pad = vxs_pad[:, :max(ilens)]  # for data parallel
        vsrc_mask = (~make_pad_mask(ilens.tolist())).to(vxs_pad.device).unsqueeze(-2)
        vhs_pad, vhs_mask = self.vencoder(vxs_pad, vsrc_mask, audio_length)
        self.vhs_pad = vhs_pad

        # 1. forward aencoder
        rms_pad = rms_pad[:, :max(ilens)]  # for data parallel
        rmsrc_mask = (~make_pad_mask(ilens.tolist())).to(rms_pad.device).unsqueeze(-2)
        arms_pad = rms_pad[:, :, :11]
        vrms_pad = rms_pad[:, :, -7:]
        armhs_pad, armhs_mask = self.armencoder(arms_pad, rmsrc_mask)
        vrmhs_pad, vrmhs_mask = self.vrmencoder(vrms_pad, rmsrc_mask)
        ctcinfo = torch.cat((armhs_pad, vrmhs_pad), dim=-1)

        # 2. forward decoder
        ys_in_pad, ys_out_pad = self.add_sos_eos(ys_pad)
        ys_mask = self.target_mask(ys_in_pad)
        apred_pad, apred_mask, armored = self.adecoder(ys_in_pad, ys_mask, ahs_pad, ahs_mask, armhs_pad)
        vpred_pad, vpred_mask, vrmpred = self.vdecoder(ys_in_pad, ys_mask, vhs_pad, vhs_mask, vrmhs_pad)


        transinfo = torch.cat((armored, vrmpred), dim=-1)
        
        cattransfeats = torch.cat((torch.softmax(apred_pad, dim=-1), torch.softmax(vpred_pad, dim=-1), transinfo), dim=-1)
        pred_pad = self.transformerweightnet(cattransfeats)



        # 3. compute attenttion loss
        loss_att = self.criterion(pred_pad, ys_out_pad)
        self.acc = th_accuracy(pred_pad.view(-1, self.odim), ys_out_pad,
                               ignore_label=self.ignore_id)

        # TODO(karita) show predicted text
        # TODO(karita) calculate these stats
        cer_ctc = None
        if self.mtlalpha == 0.0:
            loss_ctc = None
        else:
            batch_size = axs_pad.size(0)
            ahs_len = ahs_mask.view(batch_size, -1).sum(1)
            ahs_pad, ahs_mask = self.actcencoder(ahs_pad, ahs_mask)
            vhs_pad, vhs_mask = self.vctcencoder(vhs_pad, vhs_mask)

            loss_ctc = self.ctc(ahs_pad, vhs_pad, ctcinfo, ahs_len, ys_pad)
            if self.error_calculator is not None:
                ys_hat = self.ctc.argmax(avhs_pad).data
                cer_ctc = self.error_calculator(ys_hat.cpu(), ys_pad.cpu(), is_ctc=True)

        # 5. compute cer/wer
        if self.training or self.error_calculator is None:
            cer, wer = None, None
        else:
            ys_hat = pred_pad.argmax(dim=-1)
            cer, wer = self.error_calculator(ys_hat.cpu(), ys_pad.cpu())

        # copyied from e2e_asr
        alpha = self.mtlalpha
        if alpha == 0:
            self.loss = loss_att
            loss_att_data = float(loss_att)
            loss_ctc_data = None
        elif alpha == 1:
            self.loss = loss_ctc
            loss_att_data = None
            loss_ctc_data = float(loss_ctc)
        else:
            self.loss = alpha * loss_ctc + (1 - alpha) * loss_att
            loss_att_data = float(loss_att)
            loss_ctc_data = float(loss_ctc)

        loss_data = float(self.loss)
        if loss_data < CTC_LOSS_THRESHOLD and not math.isnan(loss_data):
            self.reporter.report(loss_ctc_data, loss_att_data, self.acc, cer_ctc, cer, wer, loss_data)
        else:
            logging.warning('loss (=%f) is not correct', loss_data)
        return self.loss

    def scorers(self):
        return dict(decoder=self.decoder, ctc=CTCPrefixScorer(self.ctc, self.eos))

    def aencode(self, afeat):
        self.eval()
        afeat = torch.as_tensor(afeat).unsqueeze(0)
        aenc_output, _ = self.aencoder(afeat, None)
        return aenc_output.squeeze(0)

    def vencode(self, vfeat, audiolength):
        self.eval()
        vfeat = torch.as_tensor(vfeat).unsqueeze(0)
        venc_output, _ = self.vencoder(vfeat, None, audiolength)
        return venc_output.squeeze(0)
    def armencode(self, rm):
        self.eval()
        rm = torch.as_tensor(rm).unsqueeze(0)
        rm_output, _ = self.armencoder(rm, None)
        return rm_output.squeeze(0)
    def vrmencode(self, rm):
        self.eval()
        rm = torch.as_tensor(rm).unsqueeze(0)
        rm_output, _ = self.vrmencoder(rm, None)
        return rm_output.squeeze(0)
    def transformerweightnets(self, rm):
        self.eval()
        rm = rm.unsqueeze(0)
        rm_output = self.transformerweightnet(rm)
        return rm_output.squeeze(0)
    def actcencode(self, avhs_pad):
        self.eval()
        avhs_pad = torch.as_tensor(avhs_pad).unsqueeze(0)
        avhs_output, _ = self.actcencoder(avhs_pad, None)
        return avhs_output.squeeze(0)
    def vctcencode(self, avhs_pad):
        self.eval()
        avhs_pad = torch.as_tensor(avhs_pad).unsqueeze(0)
        avhs_output, _ = self.vctcencoder(avhs_pad, None)
        return avhs_output.squeeze(0)


    def recognize(self, afeat, vfeat, rms, recog_args, char_list=None, rnnlm=None, use_jit=False):
        '''recognize feat

        :param ndnarray x: input acouctic feature (B, T, D) or (T, D)
        :param namespace recog_args: argment namespace contraining options
        :param list char_list: list of characters
        :param torch.nn.Module rnnlm: language model module
        :return: N-best decoding results
        :rtype: list

        TODO(karita): do not recompute previous attention for faster decoding
        '''
        arms = rms[:, :11]
        vrms = rms[:, -7:]
        audiolength = len(afeat)#[0]
        aenc_output = self.aencode(afeat).unsqueeze(0)
        venc_output = self.vencode(vfeat, audiolength).unsqueeze(0)
        arm_output = self.armencode(np.float32(arms)).unsqueeze(0)
        vrm_output = self.vrmencode(np.float32(vrms)).unsqueeze(0)

        ctcinfos = torch.cat((arm_output, vrm_output), dim=-1)


        '''avenc_output = torch.unsqueeze(ctcweight[:, :, 0], 2).mul(aenc_output) + torch.unsqueeze(ctcweight[:, :, 1], 2).mul(venc_output)'''
        actc_output = self.actcencode(aenc_output)
        vctc_output = self.vctcencode(venc_output)

        #avenc_output, _ = self.ctcencoders(avenc_output, None)
        if recog_args.ctc_weight > 0.0:
            lpz = self.ctc.log_softmax(actc_output, vctc_output, ctcinfos)
            lpz = lpz.squeeze(0)
        else:
            lpz = None

        h = venc_output.squeeze(0)

        logging.info('input lengths: ' + str(h.size(0)))
        # search parms
        beam = recog_args.beam_size
        penalty = recog_args.penalty
        ctc_weight = recog_args.ctc_weight

        # preprare sos
        y = self.sos
        vy = h.new_zeros(1).long()

        if recog_args.maxlenratio == 0:
            maxlen = h.shape[0]
        else:
            # maxlen >= 1
            maxlen = max(1, int(recog_args.maxlenratio * h.size(0)))
        minlen = int(recog_args.minlenratio * h.size(0))
        logging.info('max output length: ' + str(maxlen))
        logging.info('min output length: ' + str(minlen))

        # initialize hypothesis
        if rnnlm:
            hyp = {'score': 0.0, 'yseq': [y], 'rnnlm_prev': None}
        else:
            hyp = {'score': 0.0, 'yseq': [y]}
        if lpz is not None:
            import numpy

            from espnet.nets.ctc_prefix_score import CTCPrefixScore

            ctc_prefix_score = CTCPrefixScore(lpz.detach().numpy(), 0, self.eos, numpy)
            hyp['ctc_state_prev'] = ctc_prefix_score.initial_state()
            hyp['ctc_score_prev'] = 0.0
            if ctc_weight != 1.0:
                # pre-pruning based on attention scores
                from espnet.nets.pytorch_backend.rnn.decoders import CTC_SCORING_RATIO
                ctc_beam = min(lpz.shape[-1], int(beam * CTC_SCORING_RATIO))
            else:
                ctc_beam = lpz.shape[-1]
        hyps = [hyp]
        ended_hyps = []

        import six
        traced_decoder = None
        for i in six.moves.range(maxlen):
            logging.debug('position ' + str(i))

            hyps_best_kept = []
            for hyp in hyps:
                vy.unsqueeze(1)
                vy[0] = hyp['yseq'][i]

                # get nbest local scores and their ids
                ys_mask = subsequent_mask(i + 1).unsqueeze(0)
                ys = torch.tensor(hyp['yseq']).unsqueeze(0)
                # FIXME: jit does not match non-jit result
                if use_jit:
                    if traced_decoder is None:
                        traced_decoder = torch.jit.trace(self.decoder.recognize, (ys, ys_mask, enc_output))
                    local_att_scores = traced_decoder(ys, ys_mask, enc_output)
                else:
                    #local_att_scores = self.decoder.recognize(ys, ys_mask, aenc_output, venc_output, rm_output)
                    #a_att_scores, aweight = self.adecoder.recognize(ys, ys_mask, aenc_output, rm_output)
                    #v_att_scores, vweight = self.vdecoder.recognize(ys, ys_mask, venc_output, rm_output)
                    a_att_scores, armpred = self.adecoder.recognize(ys, ys_mask, aenc_output, arm_output)
                    v_att_scores, vrmpred = self.vdecoder.recognize(ys, ys_mask, venc_output, vrm_output)
                    transinfos = torch.cat((armpred, vrmpred), -1)
                    cattransfeats = torch.cat(
                        (a_att_scores, v_att_scores, transinfos), dim=-1)
                    local_att_scores = self.transformerweightnets(cattransfeats)


                if rnnlm:
                    rnnlm_state, local_lm_scores = rnnlm.predict(hyp['rnnlm_prev'], vy)
                    local_scores = local_att_scores + recog_args.lm_weight * local_lm_scores
                else:
                    local_scores = local_att_scores

                if lpz is not None:
                    local_best_scores, local_best_ids = torch.topk(
                        local_att_scores, ctc_beam, dim=1)
                    ctc_scores, ctc_states = ctc_prefix_score(
                        hyp['yseq'], local_best_ids[0], hyp['ctc_state_prev'])
                    attlog = local_att_scores[:, local_best_ids[0]]
                    ctclog = torch.from_numpy(ctc_scores - hyp['ctc_score_prev'])
                    attw, ctcw = cal_weights(attlog, ctclog, ctc_beam)
                    local_scores = \
                        attw * local_att_scores[:, local_best_ids[0]] \
                        + ctcw * torch.from_numpy(ctc_scores - hyp['ctc_score_prev'])
                    if rnnlm:
                        local_scores += recog_args.lm_weight * local_lm_scores[:, local_best_ids[0]]
                    local_best_scores, joint_best_ids = torch.topk(local_scores, beam, dim=1)
                    local_best_ids = local_best_ids[:, joint_best_ids[0]]
                else:
                    local_best_scores, local_best_ids = torch.topk(local_scores, beam, dim=1)

                for j in six.moves.range(beam):
                    new_hyp = {}
                    new_hyp['score'] = hyp['score'] + float(local_best_scores[0, j])
                    new_hyp['yseq'] = [0] * (1 + len(hyp['yseq']))
                    new_hyp['yseq'][:len(hyp['yseq'])] = hyp['yseq']
                    new_hyp['yseq'][len(hyp['yseq'])] = int(local_best_ids[0, j])
                    if rnnlm:
                        new_hyp['rnnlm_prev'] = rnnlm_state
                    if lpz is not None:
                        new_hyp['ctc_state_prev'] = ctc_states[joint_best_ids[0, j]]
                        new_hyp['ctc_score_prev'] = ctc_scores[joint_best_ids[0, j]]
                    # will be (2 x beam) hyps at most
                    hyps_best_kept.append(new_hyp)

                hyps_best_kept = sorted(
                    hyps_best_kept, key=lambda x: x['score'], reverse=True)[:beam]

            # sort and get nbest
            hyps = hyps_best_kept
            logging.debug('number of pruned hypothes: ' + str(len(hyps)))
            if char_list is not None:
                logging.debug(
                    'best hypo: ' + ''.join([char_list[int(x)] for x in hyps[0]['yseq'][1:]]))

            # add eos in the final loop to avoid that there are no ended hyps
            if i == maxlen - 1:
                logging.info('adding <eos> in the last postion in the loop')
                for hyp in hyps:
                    hyp['yseq'].append(self.eos)

            # add ended hypothes to a final list, and removed them from current hypothes
            # (this will be a probmlem, number of hyps < beam)
            remained_hyps = []
            for hyp in hyps:
                if hyp['yseq'][-1] == self.eos:
                    # only store the sequence that has more than minlen outputs
                    # also add penalty
                    if len(hyp['yseq']) > minlen:
                        hyp['score'] += (i + 1) * penalty
                        if rnnlm:  # Word LM needs to add final <eos> score
                            hyp['score'] += recog_args.lm_weight * rnnlm.final(
                                hyp['rnnlm_prev'])
                        ended_hyps.append(hyp)
                else:
                    remained_hyps.append(hyp)

            # end detection
            from espnet.nets.e2e_asr_common import end_detect
            if end_detect(ended_hyps, i) and recog_args.maxlenratio == 0.0:
                logging.info('end detected at %d', i)
                break

            hyps = remained_hyps
            if len(hyps) > 0:
                logging.debug('remeined hypothes: ' + str(len(hyps)))
            else:
                logging.info('no hypothesis. Finish decoding.')
                break

            if char_list is not None:
                for hyp in hyps:
                    logging.debug(
                        'hypo: ' + ''.join([char_list[int(x)] for x in hyp['yseq'][1:]]))

            logging.debug('number of ended hypothes: ' + str(len(ended_hyps)))

        nbest_hyps = sorted(
            ended_hyps, key=lambda x: x['score'], reverse=True)[:min(len(ended_hyps), recog_args.nbest)]

        # check number of hypotheis
        if len(nbest_hyps) == 0:
            logging.warning('there is no N-best results, perform recognition again with smaller minlenratio.')
            # should copy becasuse Namespace will be overwritten globally
            recog_args = Namespace(**vars(recog_args))
            recog_args.minlenratio = max(0.0, recog_args.minlenratio - 0.1)
            return self.recognize(feat, recog_args, char_list, rnnlm)

        logging.info('total log probability: ' + str(nbest_hyps[0]['score']))
        logging.info('normalized log probability: ' + str(nbest_hyps[0]['score'] / len(nbest_hyps[0]['yseq'])))
        return nbest_hyps

    def calculate_all_attentions(self, axs_pad, vxs_pad, rms_pad, ilens, ys_pad):
        '''E2E attention calculation

        :param torch.Tensor xs_pad: batch of padded input sequences (B, Tmax, idim)
        :param torch.Tensor ilens: batch of lengths of input sequences (B)
        :param torch.Tensor ys_pad: batch of padded character id sequence tensor (B, Lmax)
        :return: attention weights with the following shape,
            1) multi-head case => attention weights (B, H, Lmax, Tmax),
            2) other case => attention weights (B, Lmax, Tmax).
        :rtype: float ndarray
        '''
        with torch.no_grad():
            self.forward(axs_pad, vxs_pad, rms_pad, ilens, ys_pad)
        ret = dict()
        for name, m in self.named_modules():
            if isinstance(m, MultiHeadedAttention) or isinstance(m, transfMultiHeadedAttention):
                try:
                    ret[name] = m.attn.cpu().numpy()
                except:
                    pass
        return ret
