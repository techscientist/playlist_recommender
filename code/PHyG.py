#!/usr/bin/env python
"""(P)ersonalized (Hy)pergraph (P)laylist model"""

import numpy as np
import scipy.sparse
import scipy.optimize
import scipy.misc

from joblib import Parallel, delayed

from sklearn.base import BaseEstimator

__EXP_BOUND = 80.0


class PlaylistModel(BaseEstimator):

    def __init__(self, n_factors=8, edge_reg=1.0, bias_reg=1.0, user_reg=1.0,
                 song_reg=1.0, max_iter=10, edge_init=None, bias_init=None,
                 user_init=None, song_init=None, params='ebus', n_neg=64,
                 max_admm_iter=50,
                 n_jobs=1,
                 memory=None):
        """Initialize a personalized playlist model

        :parameters:
         - n_factors : int >= 0
            Number of latent factors in the personalized model

         - edge_reg : float > 0
            Variance reg on the edge weights

         - bias_reg : float > 0
            Variance reg on song bias

         - user_reg : float > 0
            Variance reg on user latent factors

         - song_reg : float > 0
            Variance reg on song latent factors

         - max_iter : int > 0
            Number of optimization steps

         - edge_init : ndarray shape=(n_features,) or None
            Initial value of edge weight vector

         - bias_init : ndarray shape=(n_songs,) or None
            Initial value of song bias vector

         - user_init : None or ndarray, shape=(n_users, n_factors)
            Initial value of user latent factor matrix

         - song_init : None or ndarray, shape=(n_songs, n_factors)
            Initial value of song latent factor matrix

         - params : str
            Which parameters to fit:
            - 'e' : edge weights
            - 'b' : song bias
            - 'u' : users
            - 's' : songs

         - n_neg : int > 0
            Number of negative examples to draw for each user sub-problem.

         - max_admm_iter : int > 0
            Maximum number of ADMM iterations to run in the inner loop.
            Set to np.inf for full convergence tests.

         - n_jobs : int
            Maximum number of jobs to run in parallel


         - memory : None or joblib.Memory
            optional memory cache object

        """

        if 'u' not in params or 'v' not in params:
            n_factors = 0

        self.max_iter = max_iter
        self.max_admm_iter = max_admm_iter
        self.n_jobs = n_jobs

        self.params = params
        self.n_factors = n_factors
        self.n_neg = n_neg

        self.w_ = edge_init
        self.b_ = bias_init
        self.u_ = user_init
        self.v_ = song_init

        self.edge_reg = edge_reg
        self.bias_reg = bias_reg
        self.user_reg = user_reg
        self.song_reg = song_reg

        if user_init is not None:
            self.n_factors = user_init.shape[1]

        if song_init is not None:
            self.n_factors = song_init.shape[1]

    def _fit_users(self, bigrams):
        # Solve over all users in parallel
        self.u_[:] = Parallel(n_jobs=self.n_jobs)(delayed(user_optimize)(self.n_neg,
                                                                         self.H_,
                                                                         self.w_,
                                                                         self.user_reg,
                                                                         self.v_,
                                                                         self.b_,
                                                                         y)
                                                  for y in bigrams)

    def _fit_songs(self, bigrams):

        # 1. generate noise instances:
        #   for each user subproblem, we need:
        #       y[i]        # pos/neg labels for each of ids_i
        #       weights[i]  # importance weights for each of ids_i
        #       ids[i]      # indices of items for this subproblem
        #

        n_songs = len(self.H_)

        subproblems = Parallel(n_jobs=self.n_jobs)(delayed(generate_user_instance)(self.n_neg,
                                                                                   self.H_,
                                                                                   self.w_,
                                                                                   y,
                                                                                   b=self.b_)
                                                   for y in bigrams)

        # 2. collect usage statistics over subproblems
        #       Z[i] = count subproblems using item i
        #

        counts = np.zeros(n_songs)
        duals = []
        Ai = []

        V = self.v_.copy()

        for sp in subproblems:
            ids_i = sp[-1]
            counts[ids_i] += 1.0
            duals.append(np.zeros((len(ids_i), self.n_factors)))
            Ai.append(np.take(V, ids_i, axis=0))

        # 3. initialize ADMM parameters
        #   a. V <- self.v_
        #   b. Lambda[i] = np.zeros( (len(ids_i), self.n_factors) )
        #   c. rho = rho_init
        #

        rho = 1.0

        # 4. ADMM loop
        #   a. [parallel] solve each user problem:
        #           (i, S[i], y[i], weights[i], Lambda[i], rho, U, V, b)
        #
        #           equivalently, pass ids, so that
        #               S*V == V[ids] == np.take(V, ids, axis=0)
        #           this buys about a 4x speedup
        #
        #           (i, y[i], weights[i], ids, Lambda[i], rho, U, V, b)
        #

        for step in range(self.max_iter_admm):
            Ai = Parallel(n_jobs=self.n_jobs)(delayed(item_factor_optimize)(i,
                                                                            subproblems[i][0],
                                                                            subproblems[i][1],
                                                                            subproblems[i][2],
                                                                            duals[i],
                                                                            rho,
                                                                            self.u_,
                                                                            V,
                                                                            self.b_,
                                                                            Aout=Ai[i])
                                              for i in range(len(subproblems)))

            # Kill the old V
            V.fill(0.0)
            for sp_i, a_i, d_i in zip(subproblems, Ai, duals):
                ids_i = sp_i[-1]
                V[ids_i, :] += (a_i + d_i)

            # Compute the normalization factor
            my_norm = 1.0/(counts + self.song_reg / rho)

            # Broadcast the normalization
            V[:] = my_norm * V

            # Update the residual*
            for sp_i, a_i, d_i in zip(subproblems, Ai, duals):
                ids_i = sp_i[-1]
                d_i[:] = d_i + a_i - np.take(V, ids_i, axis=0)

        self.v_[:] = V

    def _fit_bias(self, bigrams):

        n_songs = len(self.H_)

        # Generate the sub-problem instances
        subproblems = Parallel(n_jobs=self.n_jobs)(delayed(generate_user_instance)(self.n_neg,
                                                                                   self.H_,
                                                                                   self.w_,
                                                                                   y,
                                                                                   U=self.u_,
                                                                                   V=self.v_,
                                                                                   user_id=i)
                                                   for i, y in enumerate(bigrams))

        # Collect usage statistics over subproblems
        counts = np.zeros(n_songs)
        duals = []
        ci = []

        b = self.b_.copy()

        for sp in subproblems:
            ids_i = sp[-1]
            counts[ids_i] += 1.0
            duals.append(np.zeros(len(ids_i)))
            ci.append(np.take(b, ids_i))

        # Initialize ADMM parameters

        rho = 1.0

        for step in range(self.max_iter_admm):
            ci = Parallel(n_jobs=self.n_jobs)(delayed(item_bias_optimize)(i,
                                                                          subproblems[i][0],
                                                                          subproblems[i][1],
                                                                          subproblems[i][2],
                                                                          duals[i],
                                                                          rho,
                                                                          self.u_,
                                                                          self.v_,
                                                                          b,
                                                                          Cout=ci[i])
                                              for i in range(len(subproblems)))

            # Kill the old b
            b.fill(0.0)
            for sp_i, c_i, d_i in zip(subproblems, ci, duals):
                ids_i = sp_i[-1]
                b[ids_i] += c_i + d_i

            # Compute the normalization factor
            my_norm = 1.0/(counts + self.bias_reg / rho)

            b[:] = my_norm * b

            # Update the residuals
            for sp_i, c_i, d_i in zip(subproblems, ci, duals):
                ids_i = sp_i[-1]
                d_i[:] = d_i + c_i - np.take(b, ids_i)

        # Save the results
        self.b_[:] = b

    def _fit_edges(self, bigrams):
        '''Update the edge weights'''

        # The edge weights
        Z = 0

        # num_usage[s] counts bigrams of the form (s, .)
        num_usage = 0

        # num_playlists counts all playlists
        num_playlists = 0

        # Scatter-gather the bigram statistics over all users
        for Z_i, nu_i, np_i in Parallel(n_jobs=self.n_jobs)(delayed(edge_user_weights)(self.H_,
                                                                    self.H_T_,
                                                                    self.u_,
                                                                    idx,
                                                                    self.v_,
                                                                    self.b_, y)
                                         for (idx, y) in enumerate(bigrams)):
            Z += Z_i
            num_usage += nu_i
            num_playlists += np_i

        Z = np.asarray(Z.todense()).ravel()
        num_usage = np.asarray(num_usage.todense()).ravel()

        def __edge_objective(w):

            obj = self.edge_reg * 0.5 * np.sum(w**2)
            grad = self.edge_reg * w

            obj += - Z.dot(w)
            grad += -Z

            lse_w = scipy.misc.logsumexp(w)
            exp_w = np.exp(w)
            Hexpw = self.H_.multiply(exp_w)

            # Compute stable item-wise log-sum-exp slices
            Hexpw_norm = np.empty_like(num_usage)
            Hexpw_norm[:] = [scipy.misc.logsumexp(np.take(w, hid.indices))
                             for hid in self.H_]

            obj += num_usage.dot(Hexpw_norm) + num_playlists * lse_w

            grad += np.ravel((num_usage * np.exp(-Hexpw_norm)).dot(Hexpw))
            grad += exp_w * (num_playlists * np.exp(-lse_w))

            return obj, grad

        w0 = self.w_.copy()

        bounds = [(-__EXP_BOUND, __EXP_BOUND)] * len(w0)
        w_opt, value, diag = scipy.optimize.fmin_l_bfgs_b(__edge_objective,
                                                          w0,
                                                          bounds=bounds)

        # Ensure that convergence happened correctly
        assert diag['warnflag'] == 0

        self.w_[:] = w_opt

    def fit(self, playlists, H):
        '''fit the model.

        :parameters:
          - playlists : dict (users => list of playlists)

            eg,
                playlists = {'bm106': [ [23, 35, 41, 32, 39],
                                        [18, 19, 72, 4],
                                        [12, 9] ] }

          - H : scipy.sparse.csr_matrix (n_songs, n_edges)
        '''

        # Stash the hypergraph and its transpose
        self.H_ = H.tocsr()
        self.H_T_ = H.T.tocsr()

        # Convert playlists to bigrams
        self.user_map_, bigrams = make_bigrams(playlists)

        # Initialize edge weights
        if self.w_ is None:
            self.w_ = np.zeros(len(self.H_T_))

        if self.b_ is None:
            self.b_ = np.zeros(len(self.H_))

        if self.n_factors and (self.u_ is None):
            self.u_ = np.zeros((len(playlists), self.n_factors))

        if self.n_factors and (self.v_ is None):
            self.v_ = np.zeros((len(self.H_), self.n_factors))

        # Training loop

        for iteration in range(self.max_iter):
            # Order of operations:

            if 'e' in self.params:
                self._fit_edges(bigrams)

            if 'b' in self.params:
                self._fit_bias(bigrams)

            if 'u' in self.params:
                self._fit_users(bigrams)

            if 's' in self.params:
                self._fit_songs(bigrams)


def make_bigrams(playlists):
    '''generate user map and bigram lists.

    input:
        - playlists : dict : user_id => playlists

    '''

    user_map = dict()

    bigrams = []

    for i, (user_id, user_playlists) in enumerate(playlists.iteritems()):

        user_map[user_id] = i

        user_bigrams = []
        for pl in user_playlists:
            # Push a None onto the front
            my_pl = [None]
            my_pl.extend(pl)

            user_bigrams.extend(zip(my_pl[:-1], my_pl[1:]))

        bigrams.append(user_bigrams)

    return user_map, bigrams


# Static methods: things that can parallelize


# common functions to user, item, and bias optimization:
def make_bigram_weights(H, s, t, weight):
    if s is None:
        # This is a phantom state, so we only care about t
        my_weight = H[t].multiply(weight)
    else:
        # Otherwise, (s,t) is a valid transition, so use both
        my_weight = H[s].multiply(H[t]).multiply(weight)

    # Normalize the edge probabilities
    my_weight /= my_weight.sum()
    return my_weight


def sample_noise_items(n_neg, H, edge_dist, b, y_pos):
    '''Sample n_neg items from the noise distribution,
    forbidding observed samples y_pos.
    '''

    y_forbidden = set(y_pos)

    edge_dist = np.asarray(edge_dist / edge_dist.sum())

    # Our item distribution will be softmax over bias, ignoring the user factor
    full_item_dist = np.exp(b)

    # Knock out forbidden items
    full_item_dist[list(y_forbidden)] = 0.0

    noise_ids = []

    while len(noise_ids) < n_neg:
        # Sample an edge
        edge_id = np.flatnonzero(np.random.multinomial(1, edge_dist))[0]

        item_dist = np.ravel(H[:, edge_id].T.multiply(full_item_dist))

        item_dist_norm = np.sum(item_dist)

        if not item_dist_norm:
            continue

        item_dist /= item_dist_norm

        while True:
            new_item = np.flatnonzero(np.random.multinomial(1, item_dist))[0]

            if new_item not in y_forbidden:
                break

        y_forbidden.add(new_item)
        noise_ids.append(new_item)
        full_item_dist[new_item] = 0.0

    return noise_ids


def generate_user_instance(n_neg, H, edge_dist, bigrams, b=None,
                           U=None, V=None, user_id=None):
    '''Generate a subproblem instance.

    By default, negatives will be sampled according to their bias term `b`.

    If latent factors and a user id are supplied, negatives will be sampled
    according to their unbiased, personalized scores `U[user_id].dot(V)`

    Inputs:
        - n_neg : # of negative samples
        - H : hypergraph incidence matrix
        - edge_dist : weights of the edges of H
        - b : bias factors for items
        - bigrams : list of tuples (s, t) for the user
        - U : user factors
        - V : item factors
        - user_id : index of the user

    Outputs:
        - y : +-1 label vector
        - weights : importance weights, shape=y.shape
        - ids : list of indices for the sampled points, shape=y.shape
    '''

    if None not in (user_id, U, V):
        item_scores = V.dot(U[user_id])
    else:
        item_scores = b

    # 1. Extract positive ids
    pos_ids = [t for (s, t) in bigrams]

    exp_w = scipy.sparse.lil_matrix(np.exp(edge_dist))

    # 2. Sample n_neg songs from the noise model (u=0)
    noise_ids = sample_noise_items(n_neg,
                                   H,
                                   np.ravel(exp_w.todense()),
                                   item_scores,
                                   pos_ids)

    # 3. Compute and normalize the bigram transition weights
    #   handle the special case of s==None here

    bigram_weights = np.asarray([make_bigram_weights(H, s, t, exp_w)
                                 for (s, t) in bigrams])

    # 4. Compute the importance weights for noise samples
    noise_weights = np.sum(np.asarray([[(H[i] * bg.T).todense()
                                        for i in noise_ids]
                                       for bg in bigram_weights]),
                           axis=0).ravel()

    # 5. Construct the inputs to the solver
    y = np.ones(len(pos_ids) + len(noise_ids))
    y[len(pos_ids):] = -1

    # The first bunch are positive examples, and get weight=+1
    weights = np.ones_like(y)

    # The remaining examples get noise weights
    weights[len(pos_ids):] = noise_weights

    ids = np.concatenate([pos_ids, noise_ids])

    return y, weights, ids


# make a slicing matrix from a set of ids
def make_slice(n_total, ids, dtype=np.uint16):
    '''Make a sparse (coo) row-slicing matrix from a set of ids.'''

    # S is k-by-n so that A ~= SV
    k = len(ids)
    data = np.ones(k, dtype=dtype)

    return scipy.sparse.coo_matrix((data, (range(k), ids)),
                                   shape=(k, n_total))


# edge optimization
def edge_user_weights(H, H_T, u, idx, v, b, bigrams):
    '''Compute the edge weights and transition statistics for a user.'''

    # First, compute the user-item affinities
    item_scores = v.dot(u[idx]) + b

    # Now aggregate by edge
    edge_scores = (H_T * item_scores)**(-1.0)

    # num playlists is the number of bigrams where s == None
    num_playlists = 0
    num_usage = np.zeros(len(b))
    Z = 0

    # Now sum over bigrams
    for s, t in bigrams:
        Z = Z + make_bigram_weights(H, s, t, edge_scores)
        if s is None:
            num_playlists += 1
        else:
            num_usage[s] += 1

    return Z, num_usage, num_playlists


# item optimization
def item_factor_optimize(i, y, weights, ids, dual, rho, U_, V_, b_,
                         Aout=None):

    # Slice out the relevant components of this subproblem
    u = U_[i]
    V = np.take(V_, ids, axis=0)
    b = np.take(b_, ids)

    # Compute the residual
    Z = V - dual

    def __item_obj(_a):
        '''Optimize the item objective function'''

        A = _a.view()
        A.shape = Z.shape

        scores = y * (A.dot(u) + b)

        delta = A - Z
        f = rho * 0.5 * np.sum(delta**2)
        f += weights.dot(np.logaddexp(0, -scores))

        grad = rho * delta
        grad += np.multiply.outer(-y * weights / (1.0 + np.exp(scores)), u)

        grad_out = grad.view()
        grad_out.shape = (grad.size, )

        return f, grad_out

    # Make sure our data is properly shaped
    assert len(V) == len(b)
    assert len(V) == len(y)
    assert len(V) == len(weights)

    # Probably a decent initial point
    if Aout is not None:
        a0 = Aout
    else:
        # otherwise, V slice is pretty good too
        a0 = V.copy()

    a_opt, value, diagnostic = scipy.optimize.fmin_l_bfgs_b(__item_obj, a0)

    # Ensure that convergence happened correctly
    assert diagnostic['warnflag'] == 0

    # Reshape the solution
    a_opt.shape = V.shape

    # If we have a target destination, fill it
    if Aout is not None:
        Aout[:] = a_opt
    else:
        # Otherwise, point to the new array
        Aout = a_opt

    return Aout


# bias optimization
def item_bias_optimize(i, y, weights, ids, dual, rho, U_, V_, b_, Cout=None):

    # Slice out the relevant components of this subproblem
    u = U_[i]
    V = np.take(V_, ids, axis=0)
    b = np.take(b_, ids)

    # Compute the residual
    z = b - dual

    user_scores = u.dot(V)

    def __bias_obj(c):

        scores = y * (user_scores + c)

        delta = c - z

        f = rho * 0.5 * np.sum(delta**2)

        f += weights.dot(np.logaddexp(0, -scores))

        grad = rho * delta
        grad += -y * weights / (1.0 + np.exp(scores))

        return f, grad

    # Make sure our data is properly shaped
    assert len(V) == len(b)
    assert len(V) == len(y)
    assert len(V) == len(weights)

    if Cout is not None:
        c0 = Cout
    else:
        c0 = b.copy()

    c_opt, value, diagnostic = scipy.optimize.fmin_l_bfgs_b(__bias_obj, c0)

    assert diagnostic['warnflag'] == 0

    if Cout is not None:
        Cout[:] = c_opt
    else:
        Cout = c_opt

    return Cout


# user optimization
def user_optimize(n_noise, H, w, reg, v, b, bigrams, u0=None):
    '''Optimize a user's latent factor representation

    :parameters:
        - n_noise : int > 0
          # noise items to sample

        - H : scipy.sparse.csr_matrix, shape=(n_songs, n_edges)
          The hypergraph adjacency matrix

        - w : ndarray, shape=(n_edges,)
          edge weight array

        - reg : float >= 0
          Regularization penalty

        - v : ndarray, shape=(n_songs, n_factors)
          Latent factor representation of items

        - b : ndarray, shape=(n_songs,)
          Bias terms for songs

        - bigrams : iterable of tuples (s, t)
          Observed bigrams for the user

        - u0 : None or ndarray, shape=(n_factors,)
          Optional initial value for u

    :returns:
        - u_opt : ndarray, shape=(n_factors,)
          Optimal user vector
    '''

    y, weights, ids = generate_user_instance(n_noise, H, w, bigrams, b=b)

    return user_optimize_objective(reg,
                                   np.take(v, ids, axis=0),
                                   np.take(b, ids),
                                   y,
                                   weights,
                                   u0=u0)


# TODO:   2014-09-18 10:46:30 by Brian McFee <brian.mcfee@nyu.edu>
#  refactor this to support item and bias optimization
#  include an additional parameter for the regularization term/lagrangian
def user_optimize_objective(reg, v, b, y, omega, u0=None):
    '''Optimize a user vector from a sample of positive and noise items

    :parameters:
        - reg : float >= 0
          Regularization penalty

        - v : ndarray, shape=(m, n_factors)
          Latent factor representations for sampled items

        - b : ndarray, shape=(m,)
          Bias terms for items

        - y : ndarray, shape=(m,)
          Sign matrix for items (+1 = positive association, -1 = negative)

        - omega : ndarray, shape=(m,)
          Importance weights for items

        - u0 : None or ndarray, shape=(n_factors,)
          Initial value for the user vector

    :returns:
        - u_opt : ndarray, shape=(n_factors,)
          Optimal user vector
    '''

    def __user_obj(u):
        '''Optimize the user objective function:

            min_u reg * ||u||^2
                + sum_i y[i] * omega[i] * log(1 + exp(-y * u'v[i] + b[i]))

        '''

        # Compute the scores
        scores = y * (v.dot(u) + b)

        f = reg * 0.5 * np.sum(u**2)
        f += omega.dot(np.logaddexp(0, -scores))

        grad = reg * u
        grad -= v.T.dot(y * omega / (1.0 + np.exp(scores)))

        return f, grad

    # Make sure our data is properly shaped
    assert len(v) == len(b)
    assert len(v) == len(y)
    assert len(v) == len(omega)

    if not u0:
        u0 = np.zeros(v.shape[-1])
    else:
        assert len(u0) == v.shape[-1]

    u_opt, value, diagnostic = scipy.optimize.fmin_l_bfgs_b(__user_obj, u0)

    # Ensure that convergence happened correctly
    assert diagnostic['warnflag'] == 0

    return u_opt
