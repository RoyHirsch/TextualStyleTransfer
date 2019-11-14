import copy
import math

import torch
from torch.nn import functional as F
from torch.autograd import Variable
import torch.nn as nn


class EncoderDecoder(nn.Module):
    """
    A standard Encoder-Decoder architecture. Base for this and many
    other models.
    """

    def __init__(self, encoder, decoder, src_embed, style_embed, generator):
        super(EncoderDecoder, self).__init__()
        # src and tgt have the same embadding
        self.encoder = encoder
        self.decoder = decoder
        self.src_embed = src_embed
        self.tgt_embed = src_embed
        self.style_embed = style_embed
        self.generator = generator

        self.enc_out = None

    def forward(self, src, tgt, src_mask, tgt_mask):
        "Take in and process masked src and target sequences."
        self.enc_out = self.encode(src, src_mask)
        return self.decode(self.enc_out, src_mask,
                           tgt, tgt_mask)

    def encode(self, src, src_mask):
        return self.encoder(self.src_embed(src), src_mask)

    def decode(self, memory, src_mask, tgt, tgt_mask):
        return self.decoder(self.tgt_embed(tgt), memory, src_mask, tgt_mask)

    def decode_with_style(self, encode_out, style_preds, src_mask, tgt, tgt_mask):
        ''' Add style emb to memory and decode '''
        memory = torch.cat((encode_out, self.style_embed(style_preds).unsqueeze(1)), 1)
        # Dump last state
        memory = memory[:, :-1, :]
        return self.decoder(self.tgt_embed(tgt), memory, src_mask, tgt_mask)


class Generator(nn.Module):
    "Define standard linear + softmax generation step."

    def __init__(self, d_model, vocab):
        super(Generator, self).__init__()
        self.proj = nn.Linear(d_model, vocab)

    def forward(self, x):
        return F.log_softmax(self.proj(x), dim=-1)


def clones(module, N):
    "Produce N identical layers."
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])


class LayerNorm(nn.Module):
    "Construct a layernorm module (See citation for details)."

    def __init__(self, features, eps=1e-6):
        super(LayerNorm, self).__init__()
        self.a_2 = nn.Parameter(torch.ones(features))
        self.b_2 = nn.Parameter(torch.zeros(features))
        self.eps = eps

    def forward(self, x):
        mean = x.mean(-1, keepdim=True)
        std = x.std(-1, keepdim=True)
        return self.a_2 * (x - mean) / (std + self.eps) + self.b_2


class SublayerConnection(nn.Module):
    """
    A residual connection followed by a layer norm.
    Note for code simplicity the norm is first as opposed to last.
    """

    def __init__(self, size, dropout):
        super(SublayerConnection, self).__init__()
        self.norm = LayerNorm(size)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, sublayer):
        "Apply residual connection to any sublayer with the same size."
        return x + self.dropout(sublayer(self.norm(x)))


class PositionwiseFeedForward(nn.Module):
    "Implements FFN equation."

    def __init__(self, d_model, d_ff, dropout=0.1):
        super(PositionwiseFeedForward, self).__init__()
        self.w_1 = nn.Linear(d_model, d_ff)
        self.w_2 = nn.Linear(d_ff, d_model)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        return self.w_2(self.dropout(F.relu(self.w_1(x))))


class Embeddings(nn.Module):
    def __init__(self, d_model, vocab):
        super(Embeddings, self).__init__()
        self.lut = nn.Embedding(vocab, d_model)
        self.d_model = d_model

    def forward(self, x):
        return self.lut(x) * math.sqrt(self.d_model)


class PositionalEncoding(nn.Module):
    "Implement the PE function."

    def __init__(self, d_model, dropout, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        # Compute the positional encodings once in log space.
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0.0, max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0.0, d_model, 2) *
                             -(math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)
        self.register_buffer('pe', pe)

    def forward(self, x):
        x = x + Variable(self.pe[:, :x.size(1)], requires_grad=False)
        return self.dropout(x)


def attention(query, key, value, mask=None, dropout=None):
    "Compute 'Scaled Dot Product Attention'"
    d_k = query.size(-1)
    scores = torch.matmul(query, key.transpose(-2, -1)) \
             / math.sqrt(d_k)
    if mask is not None:
        scores = scores.masked_fill(mask == 0, -1e9)
    p_attn = F.softmax(scores, dim=-1)
    if dropout is not None:
        p_attn = dropout(p_attn)
    return torch.matmul(p_attn, value), p_attn


class MultiHeadedAttention(nn.Module):
    def __init__(self, h, d_model, dropout=0.1):
        "Take in model size and number of heads."
        super(MultiHeadedAttention, self).__init__()
        assert d_model % h == 0
        # We assume d_v always equals d_k
        self.d_k = d_model // h
        self.h = h
        self.linears = clones(nn.Linear(d_model, d_model), 4)
        self.attn = None
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, query, key, value, mask=None):
        "Implements Figure 2"
        if mask is not None:
            # Same mask applied to all h heads.
            mask = mask.unsqueeze(1)
        nbatches = query.size(0)

        # 1) Do all the linear projections in batch from d_model => h x d_k
        query, key, value = \
            [l(x).view(nbatches, -1, self.h, self.d_k).transpose(1, 2)
             for l, x in zip(self.linears, (query, key, value))]

        # 2) Apply attention on all the projected vectors in batch.
        x, self.attn = attention(query, key, value, mask=mask,
                                 dropout=self.dropout)

        # 3) "Concat" using a view and apply a final linear.
        x = x.transpose(1, 2).contiguous() \
            .view(nbatches, -1, self.h * self.d_k)
        return self.linears[-1](x)


class DecoderLayer(nn.Module):
    "Decoder is made of self-attn, src-attn, and feed forward (defined below)"

    def __init__(self, size, self_attn, src_attn, feed_forward, dropout):
        super(DecoderLayer, self).__init__()
        self.size = size
        self.self_attn = self_attn
        self.src_attn = src_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(SublayerConnection(size, dropout), 3)

    def forward(self, x, memory, src_mask, tgt_mask):
        "Follow Figure 1 (right) for connections."
        m = memory
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, tgt_mask))
        x = self.sublayer[1](x, lambda x: self.src_attn(x, m, m, src_mask))
        return self.sublayer[2](x, self.feed_forward)


class EncoderLayer(nn.Module):
    "Encoder is made up of self-attn and feed forward (defined below)"

    def __init__(self, size, self_attn, feed_forward, dropout):
        super(EncoderLayer, self).__init__()
        self.self_attn = self_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(SublayerConnection(size, dropout), 2)
        self.size = size

    def forward(self, x, mask):
        "Follow Figure 1 (left) for connections."
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, mask))
        return self.sublayer[1](x, self.feed_forward)


class BasicDecoder(nn.Module):
    "Generic N layer decoder with masking."

    def __init__(self, layer, N):
        super(BasicDecoder, self).__init__()
        self.layers = clones(layer, N)
        self.norm = LayerNorm(layer.size)

    def forward(self, x, memory, src_mask, tgt_mask):
        for layer in self.layers:
            x = layer(x, memory, src_mask, tgt_mask)
        return self.norm(x)


class BasicEncoder(nn.Module):
    "Core encoder is a stack of N layers"

    def __init__(self, layer, N):
        super(BasicEncoder, self).__init__()
        self.layers = clones(layer, N)
        self.norm = LayerNorm(layer.size)

    def forward(self, x, mask):
        "Pass the input (and mask) through each layer in turn."
        for layer in self.layers:
            x = layer(x, mask)
        return self.norm(x)


class Encoder(nn.Module):
    """
    A fully functional Encoder, including embedding layer
    """

    def __init__(self, encoder, src_embed):
        super(Encoder, self).__init__()
        self.encoder = encoder
        self.src_embed = src_embed

    def forward(self, src, src_mask):
        "Take in and process masked src and target sequences."
        return self.encoder(self.src_embed(src), src_mask)


class StyleDecoder(nn.Module):
    """
    A fully functional decoder from context and style, including language
    embedding layer and style embedding layer
    """

    def __init__(self, decoder, tgt_embed, style_embed, generator):
        super(StyleDecoder, self).__init__()
        self.decoder = decoder
        self.src_embed = tgt_embed
        self.style_embed = style_embed
        self.generator = generator

    def forward(self, enc_out, style_preds, src_mask, tgt, tgt_mask):
        "Take in and process masked src and target sequences."

        add_embadding = self.style_embed(style_preds).unsqueeze(1)
        if add_embadding.ndimension() == 1:
            add_embadding = add_embadding.unsqueeze(0).unsqueeze(1)

        elif add_embadding.ndimension() == 2:
            add_embadding = add_embadding.permute(1, 0).unsqueeze(0)

        # Concatenate the style vector at the beginning of the sequence
        # Add the same to the target for complete supervision
        # TODO: validate (Roy)
        memory = torch.cat((add_embadding, enc_out), 1)
        memory = memory[:, :-1, :]  # Dump last state

        tgt_modified = torch.cat((add_embadding, self.src_embed(tgt)), 1)
        tgt_modified = tgt_modified[:, :-1, :]
        dec_out = self.decoder(tgt_modified, memory, src_mask, tgt_mask)
        return self.generator(dec_out)


class StyleTransformer(nn.Module):
    """
    An encoder that also encodes style and adds it to the representation
    """

    def __init__(self, src_vocab, tgt_vocab, N=6,
                 d_model=512, d_ff=2048, h=8, n_styles=2, dropout=0.1):
        super().__init__()
        c = copy.deepcopy
        attn = MultiHeadedAttention(h, d_model)
        ff = PositionwiseFeedForward(d_model, d_ff, dropout)
        position = PositionalEncoding(d_model, dropout)
        src_embed = Embeddings(d_model, src_vocab)
        encoder = BasicEncoder(EncoderLayer(d_model, c(attn), c(ff), dropout), N)
        style_embed = nn.Embedding(n_styles, d_model)
        generator = nn.Linear(d_model, tgt_vocab)

        self.src_embed = src_embed
        self.encoder = encoder
        self.position = position
        self.style_embed = style_embed
        self.generator = generator

        # Initialize parameters with Glorot / fan_avg.
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def encode_style(self, style_labels):
        style_embadding = self.style_embed(style_labels).unsqueeze(1)
        if style_embadding.ndimension() == 1:
            style_embadding = style_embadding.unsqueeze(0).unsqueeze(1)
        elif style_embadding.ndimension() == 2:
            style_embadding = style_embadding.permute(1, 0).unsqueeze(0)
        return style_embadding

    def forward(self, src, src_mask, style):
        "Take in and process masked src and target sequences."
        style = self.style_embed(style).unsqueeze(dim=1)
        src = self.src_embed(src)
        src = self.position(src)
        # add style before position?
        x = src + style
        enc_out = self.encoder(x, src_mask)
        return self.generator(enc_out)


def make_encoder_decoder(src_vocab, tgt_vocab, N=6,
                         d_model=512, d_ff=2048, h=8, n_styles=2, dropout=0.1):
    "Helper: Construct a model from hyperparameters."
    c = copy.deepcopy
    attn = MultiHeadedAttention(h, d_model)
    ff = PositionwiseFeedForward(d_model, d_ff, dropout)
    position = PositionalEncoding(d_model, dropout)
    embedding = nn.Sequential(Embeddings(d_model, src_vocab), c(position))

    encoder = Encoder(
        BasicEncoder(EncoderLayer(d_model, c(attn), c(ff), dropout), N),
        embedding
    )
    decoder = StyleDecoder(
        BasicDecoder(DecoderLayer(d_model, c(attn), c(attn),
                                  c(ff), dropout), N),
        embedding,
        nn.Embedding(n_styles, d_model),
        Generator(d_model, tgt_vocab)
    )

    # This was important from their code.
    # Initialize parameters with Glorot / fan_avg.
    for p in encoder.parameters():
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)
    for p in decoder.parameters():
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)
    return encoder, decoder


def load_pretrained_embedding_to_encoder(enc_model, embedding):
    ''' Helper function to modify encoder model embedding with pre-trained
        embedding like Glove. '''
    enc_model.src_embed.lut.weight.data.copy_(embedding)
    print('Loaded pre-calculated Glove embedding')
    return enc_model
