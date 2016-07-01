import chainer
from chainer import cuda
import chainer.functions as F
import chainer.links as L

import numpy as np
from crop import crop


class RAM(chainer.Chain):

    def __init__(self, n_e=128, n_h=256, in_size=28, g_size=8, n_step=6):
        super(RAM, self).__init__(
            emb_l = L.Linear(2, n_e), # embed location
            emb_x = L.Linear(g_size*g_size, n_e), # embed image
            fc_lg = L.Linear(n_e, n_h), # loc to glimpse
            fc_xg = L.Linear(n_e, n_h), # image to glimpse
            core_hh = L.Linear(n_h, n_h), # core rnn
            core_gh = L.Linear(n_h, n_h), # glimpse to core
            fc_ha = L.Linear(n_h, 10), # core to action
            fc_hl = L.Linear(n_h, 2), # core to loc
            fc_hb = L.Linear(n_h, 1), # core to baseline
        )
        self.n_h = n_h
        self.in_size = in_size
        self.g_size = g_size
        self.n_step = n_step
        self.var = 0.03
        self.stddev = 0.173

    def clear(self):
        self.loss = None
        self.accuracy = None

    def __call__(self, x, t, train=True):
        self.clear()
        bs = x.data.shape[0] # batch size
        accum_ln_p = 0

        # init internal state of core RNN h
        h = chainer.Variable(
            self.xp.zeros(shape=(bs,self.n_h), dtype=np.float32),
            volatile=not train)

        # init mean location
        m = chainer.Variable(
            self.xp.zeros(shape=(bs,2), dtype=np.float32),
            volatile=not train)

        if train:
            self.ln_var = chainer.Variable(
                (self.xp.ones(shape=(bs,2), dtype=np.float32)
                *np.log(self.var)),
                volatile=not train)

        # forward n_steps times
        for i in range(self.n_step - 1):
            h, m, ln_p = self.forward(h, x, m, train, action=False)[:3]
            if train:
                accum_ln_p += ln_p

        y, b = self.forward(h, x, m, train, action=True)[3:5]
        if train:
            accum_ln_p += ln_p

        # loss with softmax cross entropy
        self.loss = F.softmax_cross_entropy(y, t)
        self.accuracy = F.accuracy(y, t)

        # loss with reinforce rule
        if train:
            r = self.xp.where(
                self.xp.argmax(y.data,axis=1)==t.data, 1, 0)
            self.loss += F.sum((r-b) * (r-b)) / bs
            self.loss += F.sum(accum_ln_p * (r-b)) / bs

        return self.loss

    def forward(self, h, x, m, train, action):
        if train:
            # sampling l
            l = F.gaussian(mean=m, ln_var=self.ln_var)
            l = F.clip(l, -1., 1.)

            # get location policy
            l1, l2 = F.split_axis(l, indices_or_sections=2, axis=1)
            m1, m2 = F.split_axis(m, indices_or_sections=2, axis=1)
            norm = (l1-m1)*(l1-m1) + (l2-m2)*(l2-m2)
            ln_p = 0.5 * norm / self.var
            ln_p = F.reshape(ln_p, (-1,))
        else:
            l = m

        # real-valued coordinates to indices
        if self.xp == np:
            loc = l.data
        else:
            loc = self.xp.asnumpy(l.data)
        margin = self.g_size/2
        loc = (loc+1)*0.5*(self.in_size-self.g_size+1) + margin
        loc = np.clip(loc, margin, self.in_size-margin)
        loc = np.floor(loc).astype(np.int32)

        # Retina Encoding
        hg = crop(x, loc=loc, size=self.g_size)
        hg = F.relu(self.emb_x(hg))

        # Location Encoding
        hl = F.relu(self.emb_l(l))

        # Glimpse Net
        g = F.relu(self.fc_lg(hl) + self.fc_xg(hg))

        # Core Net
        h = F.relu(self.core_hh(h) + self.core_gh(g))

        # Location Net
        m = F.tanh(self.fc_hl(h))

        if action:
            # Action Net
            y = self.fc_ha(h)
            b = F.sigmoid(self.fc_hb(h))
            b = F.reshape(b, (-1,))

            if train:
                return h, m, ln_p, y, b
            else:
                return h, m, None, y, None
        else:
            if train:
                return h, m, ln_p, None, None
            else:
                return h, m, None, None, None

    def predict(self, x, init_l):
        self.clear()
        bs = 1 # batch size

        # init internal state of core RNN h
        h = chainer.Variable(
            self.xp.zeros(shape=(bs,self.n_h), dtype=np.float32),
            volatile=not train)

        # init mean location
        m = chainer.Variable(
            self.xp.asarray(init_l.reshape(bs,2)).astype(np.float32),
            volatile=not train)

        # forward n_steps times
        locs = np.array([]).reshape(0, 2)
        for i in range(self.n_step - 1):
            h, m = self.forward(h, x, m, False, action=False)[:2]
            locs = np.vstack([locs, m.data[0]])
        y = self.forward(h, x, m, False, action=True)[3]
        y = self.xp.argmax(y.data,axis=1)[0]

        if self.xp != np:
            locs = self.xp.asnumpy(locs)
            y = self.xp.asnumpy(y)

        return y, locs
