import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange,repeat


class Transformer(nn.Module):
    def __init__(self, vocab_dim, dim, atten_dim,recycle=-1):
        super().__init__()
        self.dim = dim
        self.vocab_dim = vocab_dim
        self.recycle = recycle

        self.encoder = Encoder(vocab_dim, dim, atten_dim,recycle=recycle)
        self.decoder = Decoder(vocab_dim, dim, atten_dim)
        self.MHA = MultiHeadAttention(dim, atten_dim)
        #
        self.fc = nn.Linear(dim, 1)
        
        self.criterion=nn.CrossEntropyLoss()
    def forward(self, inputs, outputs,input_mask=None,output_mask=None):
        encode = self.encoder(inputs,input_mask)
        # recycle
        if self.recycle>0:
            decode=outputs
            for _ in range(self.recycle):
                decode = self.decoder(decode)
                out = self.MHA(encode, encode, decode)
        decode = self.decoder(outputs, output_mask)

        out = self.MHA(encode, encode, decode,output_mask)

        #
        out = torch.softmax(self.fc(out), dim=-1)
        return out

    def compute_loss(self, pred, label: torch.Tensor, en_mask:torch.Tensor,smoothing=True):
        non_pad_mask = label.ne(0) 
        words=en_mask.nonzero().sum()+1

        gt=label.view(-1)
        p=torch.argmax(pred.view(-1, pred.size(-1)), dim=-1).masked_fill_(non_pad_mask.view(-1),-1)
        acc = (p== gt).sum()
        if smoothing:
            eps = 0.1
            label = F.one_hot(label, num_classes=self.vocab_dim)
            label = label * (1 - eps) + (1 - label) * eps / (self.vocab_dim - 1)
            log_prb = F.log_softmax(pred, dim=1)
            loss = -(label * log_prb).sum(dim=-1)
            loss = loss.masked_select(non_pad_mask).sum()
            # error! batch pad not same
            # pred = pred.view(-1, pred.size(-1))
            # label = label.view(-1)
            # loss = self.criterion(pred, label)
        
        return loss, acc, words


class Attention(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.tem = dim**0.5

    def forward(self, q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, mask=None):
        '''
        parallel
         m x n x l x d
        '''
        attn = torch.matmul(q/self.tem, k.transpose(3, 2))
        if mask is not None:
            mask = rearrange(mask, 'm l -> m () () l')
            assert mask.shape[-1] == attn.shape[-1], f'masked_fill same size mask:{mask.shape} attention:{attn.shape}'
            attn.masked_fill_(mask == 0, -1e9)
        attn = torch.softmax(attn, -1)
        return torch.matmul(attn, v)


class MultiHeadAttention(nn.Module):
    def __init__(self, dim, atten_dim, head=8):
        super().__init__()
        self.head = head
        self.atten = Attention(atten_dim)
        self.q_trans = nn.Linear(dim, head*atten_dim)
        self.k_trans = nn.Linear(dim, head*atten_dim)
        self.v_trans = nn.Linear(dim, head*atten_dim)
        self.fc = nn.Linear(head*atten_dim, dim)
        self.norm = nn.LayerNorm(dim)

        self.drop = nn.Dropout(0.1, inplace=True)

    def forward(self, q, k, v, mask=None):
        '''
        batch(m) x len(l) x { head(n) x atten_dim(d) }
         return m x l x dim
        '''
        Q = self.q_trans(q)
        K = self.k_trans(k)
        V = self.v_trans(v)
        Q = rearrange(Q, 'm l (n d) -> m n l d', n=self.head)
        K = rearrange(K, 'm l (n d) -> m n l d', n=self.head)
        V = rearrange(V, 'm l (n d) -> m n l d', n=self.head)
        # if mask is not None:
        #     mask=rearrange(mask,'v -> () () () v')
        atten = self.atten(Q, K, V, mask)
        atten = rearrange(atten, 'm n l d -> m l (d n)')
        atten = self.fc(atten)

        # self.drop(atten)
        atten += q
        atten = self.norm(atten)
        return atten


class FeedForward(nn.Module):
    def __init__(self, dim=512, hide_dim=2048):
        super().__init__()
        self.dim = dim
        self.fc1 = nn.Linear(dim, hide_dim)
        self.fc2 = nn.Linear(hide_dim, dim)

    def forward(self, x):
        return self.fc2(F.relu(self.fc1(x), inplace=True))


class PositionalEncoding(nn.Module):
    def __init__(self, dim, pos_len=150):
        super().__init__()

        def positional(pos, i):
            return pos/10000**((i//2)/dim)

        PE = [[positional(pos, i) for i in range(dim)]
              for pos in range(pos_len)]
        PE = torch.tensor([PE])
        PE[..., ::2] = torch.sin(PE[..., ::2])
        PE[..., 1::2] = torch.cos(PE[..., 1::2])
        self.register_buffer('PE', PE)

    def forward(self, x):
        return self.PE[:, :x.size(1)].clone().detach()


class Encoder(nn.Module):
    def __init__(self, vocab_dim, dim, atten_dim,recycle=-1):
        super().__init__()
        self.embed = nn.Embedding(vocab_dim, dim)
        self.MHA = MultiHeadAttention(dim, atten_dim)
        self.dim = dim
        self.recycle=recycle

    def forward(self, words: torch.LongTensor,mask=None):
        encode = self.embed(words)
        encode += PositionalEncoding(words.size(1),self.dim)
        # recycle n times
        if self.recycle>0:
            for _ in range(self.recycle):
                encode = self.MHA(encode, encode, encode)  # self-attention

        encode = self.MHA(encode, encode, encode,mask)  # self-attention

        return encode


class Decoder(nn.Module):
    '''
    same as encoder，but no recycling
    '''

    def __init__(self, vocab_dim, dim, atten_dim):
        super().__init__()
        self.embed = nn.Embedding(vocab_dim, dim)
        self.MHA = MultiHeadAttention(dim, atten_dim)
        self.dim = dim

    def forward(self, words: torch.LongTensor,mask=None):
        decode = self.embed(words)
        decode += PositionalEncoding(words.size(1),self.dim)
        decode = self.MHA(decode, decode, decode,mask)  # self-attention

        return decode
