import torch.nn as nn
import torch
import torch.nn.functional as F
import math
import utils.logger as logger
import numpy as np
#from layers.attention import EmbeddingAttention

class VectorQuant(nn.Module):
    """
        Input: (N, samples, n_channels, vec_len) numeric tensor
        Output: (N, samples, n_channels, vec_len) numeric tensor
    """
    def __init__(self, n_channels, n_classes, vec_len, num_group, num_sample, normalize=False):
        super().__init__()
        if normalize:
            target_scale = 0.06
            self.embedding_scale = target_scale
            self.normalize_scale = target_scale
        else:
            self.embedding_scale = 1e-3
            self.normalize_scale = None
        self.embedding0 = nn.Parameter(torch.randn(n_channels, n_classes, vec_len, requires_grad=True) * self.embedding_scale)
        self.offset = torch.arange(n_channels).cuda() * n_classes
        # self.offset: (n_channels) long tensor
        self.n_classes = n_classes
        self.after_update()

    def forward(self, x0):
        if self.normalize_scale:
            target_norm = self.normalize_scale * math.sqrt(x0.size(3))
            x = target_norm * x0 / x0.norm(dim=3, keepdim=True)
            embedding = target_norm * self.embedding0 / self.embedding0.norm(dim=2, keepdim=True)
        else:
            x = x0
            embedding = self.embedding0
        #logger.log(f'std[x] = {x.std()}')
        x1 = x.reshape(x.size(0) * x.size(1), x.size(2), 1, x.size(3))
        # x1: (N*samples, n_channels, 1, vec_len) numeric tensor

        # Perform chunking to avoid overflowing GPU RAM.
        index_chunks = []
        for x1_chunk in x1.split(512, dim=0):
            index_chunks.append((x1_chunk - embedding).norm(dim=3).argmin(dim=2))
        index = torch.cat(index_chunks, dim=0)
        # index: (N*samples, n_channels) long tensor
        if True: # compute the entropy
            hist = index.float().cpu().histc(bins=self.n_classes, min=-0.5, max=self.n_classes - 0.5)
            prob = hist.masked_select(hist > 0) / len(index)
            entropy = - (prob * prob.log()).sum().item()
            #logger.log(f'entrypy: {entropy:#.4}/{math.log(self.n_classes):#.4}')
        else:
            entropy = 0
        index1 = (index + self.offset).view(index.size(0) * index.size(1))
        # index1: (N*samples*n_channels) long tensor
        output_flat = embedding.view(-1, embedding.size(2)).index_select(dim=0, index=index1)
        # output_flat: (N*samples*n_channels, vec_len) numeric tensor
        output = output_flat.view(x.size())

        out0 = (output - x).detach() + x
        out1 = (x.detach() - output).float().norm(dim=3).pow(2)
        out2 = (x - output.detach()).float().norm(dim=3).pow(2) + (x - x0).float().norm(dim=3).pow(2)
        #logger.log(f'std[embedding0] = {self.embedding0.view(-1, embedding.size(2)).index_select(dim=0, index=index1).std()}')
        return (out0, out1, out2, entropy)

    def after_update(self):
        if self.normalize_scale:
            with torch.no_grad():
                target_norm = self.embedding_scale * math.sqrt(self.embedding0.size(2))
                self.embedding0.mul_(target_norm / self.embedding0.norm(dim=2, keepdim=True))


class VectorQuantGroup(nn.Module):
    """
        Input: (N, samples, n_channels, vec_len) numeric tensor
        Output: (N, samples, n_channels, vec_len) numeric tensor
    """
    def __init__(self, n_channels, n_classes, vec_len, num_group, num_sample, normalize=False):
        super().__init__()
        if normalize:
            target_scale = 0.06
            self.embedding_scale = target_scale
            self.normalize_scale = target_scale
        else:
            self.embedding_scale = 1e-3
            self.normalize_scale = None

        self.n_classes = n_classes
        self._num_group = num_group
        self._num_sample = num_sample
        if not self.n_classes % self._num_group == 0:
            raise ValueError(f'num of embeddings in each group should be an integer (n_classes % num_group == 0) '
                             f'\nVectorQuantGroup n_classes=={self.n_classes}, _num_group=={self._num_group}')
        self._num_classes_per_group = int(self.n_classes / self._num_group)

        print("VectorQuantGroup:")
        print(f"n_classes:{n_classes}")
        print(f"num_group:{num_group}")
        print(f"num_sample:{num_sample}")
        assert n_classes == num_group * num_sample

        # vqvae codebook / embedding table
        self.embedding0 = nn.Parameter(torch.randn(n_channels, n_classes, vec_len, requires_grad=True) * self.embedding_scale)
        self.offset = torch.arange(n_channels).cuda() * n_classes
        # self.offset: (n_channels) long tensor
        self.after_update()

    def forward(self, x0):
        # print("inside VectorQuantGroup forward():")
        #
        # print("x0.size()", x0.size())  # [30, 2701, 1, 128]

        if self.normalize_scale:
            target_norm = self.normalize_scale * math.sqrt(x0.size(3))
            x = target_norm * x0 / x0.norm(dim=3, keepdim=True)
            embedding = target_norm * self.embedding0 / self.embedding0.norm(dim=2, keepdim=True)
        else:
            x = x0
            embedding = self.embedding0
        #logger.log(f'std[x] = {x.std()}')
        x1 = x.reshape(x.size(0) * x.size(1), x.size(2), 1, x.size(3))
        # x1: (N*samples, n_channels, 1, vec_len) numeric tensor

        # Perform chunking to avoid overflowing GPU RAM.
        index_chunks = []
        index_chunks_atom = []  # used to hold the discrete atom symbols!
        index_chunks_group = []  # used to hold the discrete group symbols!
        prob_chunks = []
        for x1_chunk in x1.split(512, dim=0):
            d = (x1_chunk - embedding).norm(dim=3)

            # print("x1_chunk.size()", x1_chunk.size())  # [512, 1, 1, 128]
            # print("embedding.size()", embedding.size())  # [1, 410, 128]
            # print("(x1_chunk - embedding).size()", (x1_chunk - embedding).size())  # [512, 1, 410, 128]
            # print("d.size()", d.size())  # [512, 1, 410]

            # Find the nearest atom
            index_chunk_atom = d.argmin(dim=2)
            index_chunks_atom.append(index_chunk_atom.clone().detach())
            # print("index_chunk_atom.size()", index_chunk_atom.size())
            # print("index_chunk_atom", index_chunk_atom)

            # Compute the group-wise distance
            d_group = torch.zeros(x1_chunk.shape[0], 1, self._num_group).to(torch.device('cuda'))
            for i in range(self._num_group):
                d_group[:, :, i] = torch.mean(
                    d[:, :, i * self._num_classes_per_group: (i + 1) * self._num_classes_per_group], 2)

            # Find the nearest group
            index_chunk_group = d_group.argmin(dim=2)  # TODO return (as they are groups)
            index_chunks_group.append(index_chunk_group.clone().detach())
            # print(f"index_chunk_group.size()={index_chunk_group.size()}")  # [512,1] each element of tensor is an int from 0 to 40, representing groups
            # print(f"index_chunk_group[:10]", index_chunk_group[:10])


            # Generate mask for the nearest group
            index_chunk_group = index_chunk_group.repeat(1, self._num_classes_per_group)
            index_chunk_group = torch.mul(self._num_classes_per_group, index_chunk_group)
            idx_mtx = torch.LongTensor([x for x in range(self._num_classes_per_group)]).unsqueeze(0).cuda()
            index_chunk_group += idx_mtx
            encoding_mask = torch.zeros(x1_chunk.shape[0], self.n_classes).cuda()
            encoding_mask.scatter_(1, index_chunk_group, 1)

            # Compute the weight atoms in the group
            encoding_prob = torch.div(1, d.squeeze())

            # Apply the mask
            masked_encoding_prob = torch.mul(encoding_mask, encoding_prob)
            p, idx = masked_encoding_prob.sort(dim=1, descending=True)
            prob_chunks.append(p[:, :self._num_sample])
            index_chunks.append(idx[:, :self._num_sample])

        # combine chunks together
        index_atom = torch.cat(index_chunks_atom, dim=0)
        index_group = torch.cat(index_chunks_group, dim=0)

        # print("before view() index_atom.size()", index_atom.size())  # [N*samples, n_channels]
        # print("before view() index_atom[:10]", index_atom[:10])
        # print("before view() index_atom.min()", index_atom.min())
        # print("before view() index_atom.max()", index_atom.max())
        # print("before view() index_group.size()", index_group.size())  # [N*samples, n_channels]
        # print("before view() index_group[:10]", index_group[:10])
        # print("before view() index_group.min()", index_group.min())
        # print("before view() index_group.max()", index_group.max())

        # unflatten to reintroduce N and samples dimension
        index_atom = index_atom.view((x.size(0), x.size(1), x.size(2)))  # [N, samples, n_channels]
        index_group = index_group.view((x.size(0), x.size(1), x.size(2)))  # [N, samples, n_channels]

        # print("after view() index_atom.size()", index_atom.size())  # [N*samples, n_channels]
        # print("after view() index_atom[:10]", index_atom[:10])
        # print("after view() index_atom.min()", index_atom.min())
        # print("after view() index_atom.max()", index_atom.max())
        # print("after view() index_group.size()", index_group.size())  # [N*samples, n_channels]
        # print("after view() index_group[:10]", index_group[:10])
        # print("after view() index_group.min()", index_group.min())
        # print("after view() index_group.max()", index_group.max())

        index = torch.cat(index_chunks, dim=0)
        # print("index.size()", index.size())
        # print("index[:10]", index[:10])

        prob_dist = torch.cat(prob_chunks, dim=0)
        prob_dist = F.normalize(prob_dist, p=1, dim=1)
        # index: (N*samples, n_channels) long tensor
        if True: # compute the entropy
            hist = index[:, 0].float().cpu().histc(bins=self.n_classes, min=-0.5, max=self.n_classes - 0.5)
            prob = hist.masked_select(hist > 0) / len(index)
            entropy = - (prob * prob.log()).sum().item()
            #logger.log(f'entrypy: {entropy:#.4}/{math.log(self.n_classes):#.4}')
        else:
            entropy = 0
        index1 = (index + self.offset)
        # index1: (N*samples*n_channels) long tensor
        output_list = []
        for i in range(self._num_sample):
            # Jason Fong: seems that what is happening here is that the embeddings fed to the decoder
            # is the sum of all the embeddings in the table weighted by a probability distribution
            output_list.append(torch.mul(embedding.view(-1, embedding.size(2)).index_select(dim=0, index=index1[:, i]), prob_dist[:, i].unsqueeze(1).detach()))

        output_cat = torch.stack(output_list, dim=2)
        output_flat = torch.sum(output_cat, dim=2)
        # output_flat: (N*samples*n_channels, vec_len) numeric tensor
        output = output_flat.view(x.size())

        discrete = (output - x).detach() + x  # NB that this is the continuous vector corresponding to the discrete group
        vq_pen = (x.detach() - output).float().norm(dim=3).pow(2)
        encoder_pen = (x - output.detach()).float().norm(dim=3).pow(2) + (x - x0).float().norm(dim=3).pow(2)
        #logger.log(f'std[embedding0] = {self.embedding0.view(-1, embedding.size(2)).index_select(dim=0, index=index1).std()}')

        # print("discrete.size()", discrete.size())  # [30, 2701, 1, 128]

        return (discrete, vq_pen, encoder_pen, entropy, index_atom, index_group)  # [30, 2701, 1, 128] (generating 10 test utts for first 3 speakers, so 30 utts in total)

    def after_update(self):
        if self.normalize_scale:
            with torch.no_grad():
                target_norm = self.embedding_scale * math.sqrt(self.embedding0.size(2))
                self.embedding0.mul_(target_norm / self.embedding0.norm(dim=2, keepdim=True))
