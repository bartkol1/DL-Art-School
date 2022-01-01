import functools
from math import log

import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers import GPT2Model, GPT2Config

from models.arch_util import AttentionBlock
from models.gpt_voice.gpt_asr_hf import GPT2InferenceModel
from models.tacotron2.text import symbols
from trainer.networks import register_model
from utils.util import opt_get


def null_position_embeddings(range, dim):
    return torch.zeros((range.shape[0], range.shape[1], dim), device=range.device)


class ConditioningEncoder(nn.Module):
    def __init__(self,
                 spec_dim,
                 embedding_dim,
                 attn_blocks=6,
                 num_attn_heads=4,
                 do_checkpointing=False):
        super().__init__()
        attn = []
        self.init = nn.Conv1d(spec_dim, embedding_dim, kernel_size=1)
        for a in range(attn_blocks):
            attn.append(AttentionBlock(embedding_dim, num_attn_heads, do_checkpoint=do_checkpointing))
        self.attn = nn.Sequential(*attn)
        self.dim = embedding_dim
        self.do_checkpointing = do_checkpointing

    def forward(self, x):
        h = self.init(x)
        h = self.attn(h)
        return h[:, :, 0]


class TopEncoder(nn.Module):
    def __init__(self, layers, dim, heads, do_checkpointing=False, dim_reduction=16):
        self.init = nn.Conv1d(dim, dim, kernel_size=1)
        reduction_layers = []
        for j in range(int(log(dim_reduction, 2))):
            reduction_layers.append(AttentionBlock(dim, heads, do_checkpoint=do_checkpointing))
            reduction_layers.append(nn.Conv1d(dim, dim, kernel_size=3, padding=1, stride=2))
        self.reduction_layers = nn.Sequential(*reduction_layers)
        actual_layers = [AttentionBlock(dim, heads, do_checkpoint=do_checkpointing) for _ in range(layers)]
        self.actual_layers = nn.Sequential(*actual_layers)

    def forward(self, x):
        h = self.init(x)
        h = self.reduction_layers(h)
        h = self.actual_layers(h)
        return h


class UnifiedGptVoice(nn.Module):
    """
    Derived from GptTtsHf, but offers multiple modes of autoregressive operation:
    - Text only
    - Voice only
    - Text conditioned on voice
    - Voice conditioned on text
    """

    def __init__(self, top_encoder_layers=4, top_layers=8, bottom_layers=8, top_dim_reduction=16, model_dim=512, heads=8,
                 max_symbols_per_phrase=120, max_mel_tokens=250, max_total_tokens=370, max_conditioning_inputs=3,
                 checkpointing=True, mel_length_compression=1024, max_conditioning_length=60, number_text_tokens=256,
                 start_text_token=255, stop_text_token=0, number_mel_codes=8194, start_mel_token=8192,
                 stop_mel_token=8193):
        super().__init__()

        self.number_text_tokens = number_text_tokens
        self.start_text_token = start_text_token
        self.stop_text_token = stop_text_token
        self.number_mel_codes = number_mel_codes
        self.start_mel_token = start_mel_token
        self.stop_mel_token = stop_mel_token

        self.max_mel_tokens = max_mel_tokens
        self.max_symbols_per_phrase = max_symbols_per_phrase
        self.max_total_tokens = max_total_tokens
        self.model_dim = model_dim
        self.max_conditioning_inputs = max_conditioning_inputs
        self.mel_length_compression = mel_length_compression
        self.conditioning_encoder = ConditioningEncoder(80, model_dim, num_attn_heads=heads)
        self.text_embedding = nn.Embedding(self.number_text_tokens, model_dim)
        self.text_pos_solo_embedding = nn.Embedding(self.max_symbols_per_phrase + 1, model_dim)
        self.text_pos_paired_embedding = nn.Embedding(self.max_symbols_per_phrase + 1, model_dim)
        self.mel_pos_solo_embedding = nn.Embedding(self.max_mel_tokens + 1, model_dim)
        self.mel_pos_paired_embedding = nn.Embedding(self.max_mel_tokens + 1, model_dim)
        seq_length = 2+self.max_total_tokens+self.max_conditioning_inputs

        self.top_encoder = TopEncoder(top_encoder_layers, model_dim, heads, do_checkpointing=checkpointing,
                                      dim_reduction=top_dim_reduction)
        self.top_gpt_config = GPT2Config(vocab_size=1,
                                         n_positions=seq_length // top_dim_reduction,
                                         n_ctx=seq_length // top_dim_reduction,
                                         n_embd=model_dim,
                                         n_layer=top_layers,
                                         n_head=heads,
                                         gradient_checkpointing=checkpointing,
                                         use_cache=not checkpointing)
        self.top_gpt = GPT2Model(self.top_gpt_config)
        del self.top_gpt.wte
        self.top_gpt_start_embedding = nn.Parameter(torch.randn(1,1,model_dim)*self.top_gpt_config.initializer_range,
                                                    requires_grad=True)
        self.top_dim_reduction = top_dim_reduction

        self.bottom_gpt_config = GPT2Config(vocab_size=self.number_mel_codes,
                                            n_positions=seq_length,
                                            n_ctx=seq_length,
                                            n_embd=model_dim,
                                            n_layer=bottom_layers,
                                            n_head=heads,
                                            gradient_checkpointing=checkpointing,
                                            use_cache=not checkpointing)
        self.bottom_gpt = GPT2Model(self.bottom_gpt_config)
        # Override the built in positional embeddings
        del self.bottom_gpt.wpe
        self.bottom_gpt.wpe = functools.partial(null_position_embeddings, dim=model_dim)

        self.final_norm = nn.LayerNorm(model_dim)
        self.text_head = nn.Linear(model_dim, self.number_text_tokens)
        self.mel_head = nn.Linear(model_dim, self.number_mel_codes)
        self.max_conditioning_length = max_conditioning_length

        # Initialize the embeddings per the GPT-2 scheme
        for module in [self.text_embedding, self.text_pos_solo_embedding, self.text_pos_paired_embedding,
                       self.mel_pos_solo_embedding, self.mel_pos_paired_embedding]:
            module.weight.data.normal_(mean=0.0, std=self.bottom_gpt.config.initializer_range)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()

    def build_aligned_inputs_and_targets(self, input, start_token, stop_token):
        inp = F.pad(input, (1,0), value=start_token)
        tar = F.pad(input, (0,1), value=stop_token)
        return inp, tar

    def set_mel_padding(self, mel_input_tokens, wav_lengths):
        """
        Given mel tokens that are derived from a padded audio clip and the actual lengths of each batch element in
        that audio clip, reformats the tokens with STOP_MEL_TOKEN in place of the zero padding. This is required
        preformatting to create a working TTS model.
        """
        # Set padding areas within MEL (currently it is coded with the MEL code for <zero>).
        mel_lengths = wav_lengths // self.mel_length_compression
        for b in range(len(mel_lengths)):
            actual_end = mel_lengths[b] + 1  # Due to the convolutional nature of how these tokens are generated, it would be best if the model predicts a token past the actual last token.
            if actual_end < mel_input_tokens.shape[-1]:
                mel_input_tokens[b, actual_end:] = self.stop_mel_token
        return mel_input_tokens

    def randomly_permute_conditioning_input(self, speech_conditioning_input):
        """
        Randomly permute the conditioning spectrogram, to destroy any structure present. Note that since the
        conditioning input is derived from a discrete spectrogram, it does actually retain structure, but only a little
        bit (actually: exactly how much we want; enough to discriminate different vocal qualities, but nothing about
        what is being said).
        """
        cond_input = speech_conditioning_input[:,:,torch.randperm(speech_conditioning_input.shape[-1])]
        if cond_input.shape[-1] > self.max_conditioning_length:
            cond_input = cond_input[:,:,:self.max_conditioning_length]
        return cond_input


    def get_top_embeddings(self, embedded_input):
        true_embeddings = self.top_encoder(embedded_input)
        inputs = torch.cat([self.top_gpt_start_embedding, true_embeddings[:,:-1]], dim=1)
        top_pred = self.top_gpt(inputs_embeds=inputs, return_dict=True)
        return top_pred.last_hidden_state, true_embeddings


    def inject_top_embeddings(self, embedded_input, probability_of_true_top_embedding=.5):
        pred, true = self.get_top_embeddings(embedded_input)
        rand = torch.bernoulli(torch.full((1,embedded_input.shape[1]),
                                          fill_value=probability_of_true_top_embedding)).to(embedded_input.device)
        mix = pred * rand + true * (not rand)
        embs = torch.chunk(embedded_input, self.top_dim_reduction, dim=1)
        assert len(embs) == mix.shape[1]
        rejoin = []
        for i, emb in enumerate(embs):
            rejoin.append(torch.cat([mix[i], emb]), dim=1)
        return torch.cat(rejoin, dim=1)


    def get_logits(self, speech_conditioning_input, first_inputs, first_head, second_inputs=None, second_head=None, get_attns=False):
        if second_inputs is not None:
            emb = torch.cat([speech_conditioning_input, first_inputs, second_inputs], dim=1)
        else:
            emb = torch.cat([speech_conditioning_input, first_inputs], dim=1)

        gpt_out = self.bottom_gpt(inputs_embeds=emb, return_dict=True, output_attentions=get_attns)
        if get_attns:
            return gpt_out.attentions

        enc = gpt_out.last_hidden_state[:, 1:]  # The first logit is tied to the speech_conditioning_input
        enc = self.final_norm(enc)
        first_logits = enc[:, :first_inputs.shape[1]]
        first_logits = first_head(first_logits)
        first_logits = first_logits.permute(0,2,1)
        if second_inputs is not None:
            second_logits = enc[:, -second_inputs.shape[1]:]
            second_logits = second_head(second_logits)
            second_logits = second_logits.permute(0,2,1)
            return first_logits, second_logits
        else:
            return first_logits

    def forward(self, speech_conditioning_input, text_inputs, mel_inputs, wav_lengths, text_first=True, return_attentions=False):
        """
        Forward pass that uses both text and voice in either text conditioning mode or voice conditioning mode
        (actuated by `text_first`).

        speech_conditioning_input: MEL float tensor, (b,80,s)
        text_inputs: long tensor, (b,t)
        mel_inputs:  long tensor, (b,m)
        wav_lengths: long tensor, (b,)
        """
        assert self.max_mel_tokens >= mel_inputs.shape[1], f'{mel_inputs.shape[1]}'
        assert self.max_symbols_per_phrase >= text_inputs.shape[1], f'{text_inputs.shape[1]}'
        assert self.max_total_tokens >= mel_inputs.shape[1] + text_inputs.shape[1], f'{mel_inputs.shape[1]}, {text_inputs.shape[1]}'

        mel_inputs = self.set_mel_padding(mel_inputs, wav_lengths)
        speech_conditioning_input = self.randomly_permute_conditioning_input(speech_conditioning_input)
        speech_conditioning_input = self.conditioning_encoder(speech_conditioning_input).unsqueeze(1)

        text_inputs, text_targets = self.build_aligned_inputs_and_targets(text_inputs, self.start_text_token, self.stop_text_token)
        text_emb = self.text_embedding(text_inputs) + self.text_pos_paired_embedding(torch.arange(text_inputs.shape[1], device=text_inputs.device))
        mel_inputs, mel_targets = self.build_aligned_inputs_and_targets(mel_inputs, self.start_mel_token, self.stop_mel_token)
        mel_emb = self.bottom_gpt.get_input_embeddings()(mel_inputs)
        mel_emb = mel_emb + self.mel_pos_paired_embedding(torch.arange(mel_emb.shape[1], device=mel_emb.device))

        if text_first:
            text_logits, mel_logits = self.get_logits(speech_conditioning_input, text_emb, self.text_head, mel_emb, self.mel_head, get_attns=return_attentions)
        else:
            mel_logits, text_logits = self.get_logits(speech_conditioning_input, mel_emb, self.mel_head, text_emb, self.text_head, get_attns=return_attentions)

        if return_attentions:
            return mel_logits
        loss_text = F.cross_entropy(text_logits, text_targets.long())
        loss_mel = F.cross_entropy(mel_logits, mel_targets.long())
        return loss_text.mean(), loss_mel.mean(), mel_logits

    def text_forward(self, speech_conditioning_input, text_inputs):
        """
        Performs autoregressive modeling on only text. Still requires a speech_conditioning_input due to the way the
        model inputs are formatted. Just provide any audio clip (arguably, zeros could be provided).
        """
        assert self.max_symbols_per_phrase >= text_inputs.shape[1], f'{text_inputs.shape[1]}'

        speech_conditioning_input = self.randomly_permute_conditioning_input(speech_conditioning_input)
        speech_conditioning_input = self.conditioning_encoder(speech_conditioning_input).unsqueeze(1)

        text_inputs, text_targets = self.build_aligned_inputs_and_targets(text_inputs, self.start_text_token, self.stop_text_token)
        text_emb = self.text_embedding(text_inputs) + self.text_pos_solo_embedding(torch.arange(text_inputs.shape[1], device=text_inputs.device))
        text_logits = self.get_logits(speech_conditioning_input, text_emb, self.text_head)
        loss_text = F.cross_entropy(text_logits, text_targets.long())
        return loss_text.mean()

    def speech_forward(self, speech_conditioning_input, mel_inputs, wav_lengths):
        """
        Performs autoregressive modeling on only speech data.
        """
        assert self.max_mel_tokens >= mel_inputs.shape[1], f'{mel_inputs.shape[1]}'

        mel_inputs = self.set_mel_padding(mel_inputs, wav_lengths)
        speech_conditioning_input = self.randomly_permute_conditioning_input(speech_conditioning_input)
        speech_conditioning_input = self.conditioning_encoder(speech_conditioning_input).unsqueeze(1)

        mel_inputs, mel_targets = self.build_aligned_inputs_and_targets(mel_inputs, self.start_mel_token, self.stop_mel_token)
        mel_emb = self.bottom_gpt.get_input_embeddings()(mel_inputs)
        mel_emb = mel_emb + self.mel_pos_solo_embedding(torch.arange(mel_emb.shape[1], device=mel_emb.device))
        mel_logits = self.get_logits(speech_conditioning_input, mel_emb, self.mel_head)
        loss_mel = F.cross_entropy(mel_logits, mel_targets.long())
        return loss_mel.mean()

    def inference_speech(self, speech_conditioning_input, text_inputs, **hf_generate_kwargs):
        if not hasattr(self, 'inference_model'):
            self.inference_model = GPT2InferenceModel(self.bottom_gpt_config, self.bottom_gpt, self.mel_pos_paired_embedding, self.final_norm, self.mel_head)

        text_inputs, text_targets = self.build_aligned_inputs_and_targets(text_inputs, self.start_text_token, self.stop_text_token)
        text_emb = self.text_embedding(text_inputs) + self.text_pos_paired_embedding(torch.arange(text_inputs.shape[1], device=text_inputs.device))

        # Randomly permute the conditioning spectrogram, to destroy any structure present.
        speech_conditioning_input = self.randomly_permute_conditioning_input(speech_conditioning_input)
        cond = self.conditioning_encoder(speech_conditioning_input).unsqueeze(1)

        emb = torch.cat([cond, text_emb], dim=1)
        self.inference_model.store_mel_emb(emb)

        fake_inputs = torch.full((emb.shape[0],emb.shape[1]+1,), fill_value=1, dtype=torch.long, device=text_inputs.device)
        fake_inputs[:,-1] = self.start_mel_token

        gen = self.inference_model.generate(fake_inputs, bos_token_id=self.start_mel_token, pad_token_id=self.stop_mel_token, eos_token_id=self.stop_mel_token,
                                            max_length=self.bottom_gpt_config.n_positions, **hf_generate_kwargs)
        return gen[:, fake_inputs.shape[1]:]


@register_model
def register_unified_gpt_voice_bilevel(opt_net, opt):
    return UnifiedGptVoice(**opt_get(opt_net, ['kwargs'], {}))


if __name__ == '__main__':
    gpt = UnifiedGptVoice(model_dim=256, heads=4)
    l = gpt(torch.randn(2, 80, 800),
            torch.randint(high=len(symbols), size=(2,80)),
            torch.randint(high=8192, size=(2,250)),
            torch.tensor([150*256,195*256]))
