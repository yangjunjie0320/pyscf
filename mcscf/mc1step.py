#!/usr/bin/env python
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

import time
import copy
import tempfile
import numpy
import scipy.linalg
import pyscf.lib.logger as logger
import pyscf.scf
import casci
import aug_hessian
import mc_ao2mo

# ref. JCP, 82, 5053;  JCP, 73, 2342

def h1e_for_cas(casscf, mo, eris):
    ncas = casscf.ncas
    ncore = casscf.ncore
    nocc = ncas + ncore
    if ncore == 0:
        vhf_c = 0
    else:
        jc_aa = eris.jc_pp[:,ncore:nocc,ncore:nocc]
        kc_aa = eris.kc_pp[:,ncore:nocc,ncore:nocc]
        vhf_c = numpy.einsum('iuv->uv', jc_aa) * 2 \
              - numpy.einsum('iuv->uv', kc_aa)
    mocc = mo[:,ncore:nocc]
    h1eff = reduce(numpy.dot, (mocc.T, casscf.get_hcore(), mocc)) + vhf_c
    return h1eff

def expmat(a):
    x1 = numpy.dot(a,a)
    u = numpy.eye(a.shape[0]) + a + .5 * x1
    x2 = numpy.dot(x1,a)
    u = u + 1./6 * x2
    #x3 = numpy.dot(x1,x1)
    #u = u + 1./24 * x3
    u, w, vh = numpy.linalg.svd(u)
    return numpy.dot(u,vh)

# gradients, hessian operator and hessian diagonal
def gen_g_hop(casscf, mo, casdm1, casdm2, eris):
    ncas = casscf.ncas
    ncore = casscf.ncore
    nocc = ncas + ncore
    nmo = mo.shape[1]

    dm1 = numpy.zeros((nmo,nmo))
    idx = numpy.arange(ncore)
    dm1[idx,idx] = 2
    dm1[ncore:nocc,ncore:nocc] = casdm1

    # part2, part3
    vhf_c = numpy.einsum('ipq->pq', eris.jc_pp) * 2 \
          - numpy.einsum('ipq->pq', eris.kc_pp)
    vhf_ca = vhf_c + numpy.einsum('uvpq,uv->pq', eris.aapp, casdm1) \
                   - numpy.einsum('upqv,uv->pq', eris.appa, casdm1) * .5

    ################# gradient #################
    #hdm2 = numpy.einsum('tuvw,vwpq->tupq', casdm2, eris.aapp)
    hdm2 = numpy.dot(casdm2.reshape(ncas*ncas,-1), \
                     eris.aapp.reshape(ncas*ncas,-1)).reshape(ncas,ncas,nmo,nmo)

    h1e_mo = reduce(numpy.dot, (mo.T, casscf.get_hcore(), mo))
    g = numpy.dot(h1e_mo, dm1)
    g[:,:ncore] += vhf_ca[:,:ncore] * 2
    g[:,ncore:nocc] += numpy.einsum('vuuq->qv',hdm2[:,:,ncore:nocc]) \
            + numpy.dot(vhf_c[:,ncore:nocc],casdm1)

    ############## hessian, diagonal ###########
    # part1
    tmp = casdm2.transpose(1,2,0,3) + casdm2.transpose(0,2,1,3) # (jp|rk) *[E(jq,sk) + E(jq,ks)] => qspr
    tmp = numpy.einsum('uvtw,tpqw->upvq', tmp, eris.appa)
    hdm2 = tmp + hdm2.transpose(0,2,1,3)

    # part7
    h_diag = numpy.einsum('ii,jj->ij', h1e_mo, dm1) - h1e_mo * dm1
    h_diag = h_diag + h_diag.T

    # part8
    g_diag = g.diagonal()
    h_diag -= g_diag + g_diag.reshape(-1,1)
    idx = numpy.arange(nmo)
    h_diag[idx,idx] += g_diag * 2

    # part2, part3
    v_diag = vhf_ca.diagonal() # (pr|kl) * E(sq,lk)
    h_diag[:,:ncore] += v_diag.reshape(-1,1) * 2
    h_diag[:ncore] += v_diag * 2
    idx = numpy.arange(ncore)
    h_diag[idx,idx] -= v_diag[:ncore] * 4
    # V_{pr} E_{sq}
    tmp = numpy.einsum('ii,jj->ij', vhf_c, casdm1)
    h_diag[:,ncore:nocc] += tmp
    h_diag[ncore:nocc,:] += tmp.T

    # part4
    # -2(pr|sq) + 4(pq|sr) + 4(pq|rs) - 2(ps|rq)
    jc_c = numpy.einsum('ijj->ij', eris.jc_pp)
    kc_c = numpy.einsum('ijj->ij', eris.kc_pp)
    tmp = 6 * kc_c - 2 * jc_c
    h_diag[:,:ncore] += tmp.T
    h_diag[:ncore,:] += tmp
    h_diag[:ncore,:ncore] -= tmp[:,:ncore] * 2

    # part5 and part6 diag
    # -(qr|kp) E_s^k  p in core, sk in active
    jc_aa = eris.jc_pp[:,ncore:nocc,ncore:nocc]
    kc_aa = eris.kc_pp[:,ncore:nocc,ncore:nocc]
    tmp = numpy.einsum('jik,ik->ji', 6*kc_aa-2*jc_aa, casdm1)
    h_diag[:ncore,ncore:nocc] -= tmp
    h_diag[ncore:nocc,:ncore] -= tmp.T

    v_diag = numpy.einsum('ijij->ij', hdm2)
    h_diag[ncore:nocc,:] += v_diag
    h_diag[:,ncore:nocc] += v_diag.T

    g_orb = casscf.pack_uniq_var(g-g.T)
    h_diag = casscf.pack_uniq_var(h_diag)

    def h_op(x):
        x1 = casscf.unpack_uniq_var(x)
        x_cu = x1[:ncore,ncore:]
        x_av = x1[ncore:nocc,nocc:]
        x_ac = x1[ncore:nocc,:ncore]

        # part7
        x2 = reduce(numpy.dot, (h1e_mo, x1, dm1))
        # part8, the hessian provides
        #x2 -= numpy.dot(g, x1)
        # but it may destroy Hermitian unless g == g.T (the converged g).
        # So symmetrize it with
        # x_{pq} -= g_{pr} \delta_{qs} x_{rs} * .5
        # x_{rs} -= g_{rp} \delta_{sq} x_{pq} * .5
        #x2 -= (numpy.dot(g, x1) + numpy.dot(g.T, x1)) * .5
        x2 -= numpy.dot(g+g.T, x1) * .5
        # part2
        x2[:ncore] += numpy.dot(x_cu, vhf_ca[ncore:]) * 2
        # part3
        x2[ncore:nocc] += reduce(numpy.dot, (casdm1, x_av, vhf_c[nocc:])) \
                        + reduce(numpy.dot, (casdm1, x_ac, vhf_c[:ncore]))
        # part4, part5, part6
        if ncore > 0:
# Due to x1_rs [4(pq|sr) + 4(pq|rs) - 2(pr|sq) - 2(ps|rq)] for r>s p>q,
#    == -x1_sr [4(pq|sr) + 4(pq|rs) - 2(pr|sq) - 2(ps|rq)] for r>s p>q,
# x2[:,:ncore] += H * x1[:,:ncore] => (becuase x1=-x1.T) =>
# x2[:,:ncore] += -H' * x1[:ncore] => (becuase x2-x2.T) =>
# x2[:ncore] += H' * x1[:ncore]
            va, vc = casscf.update_jk_in_ah(mo, x1, casdm1, eris)
            x2[ncore:nocc] += va
            x2[:ncore,ncore:] += vc

        # part1
        x2[ncore:nocc] += numpy.einsum('upvr,vr->up', hdm2, x1[ncore:nocc])

        x2 = x2 - x2.T
        return casscf.pack_uniq_var(x2)
    return g_orb, h_op, h_diag


def rotate_orb_ah(casscf, mo, fcivec, e_ci, eris, dx=0, verbose=None):
    if verbose is None:
        verbose = casscf.verbose
    log = logger.Logger(casscf.stdout, verbose)

    ncas = casscf.ncas
    nelecas = casscf.nelecas
    nmo = mo.shape[1]

    t2m = (time.clock(), time.time())
    casdm1, casdm2 = casscf.fcisolver.make_rdm12(fcivec, ncas, nelecas)
    g_orb0, h_op, h_diag = casscf.gen_g_hop(mo, casdm1, casdm2, eris)
    t3m = log.timer('gen h_op', *t2m)

    precond = lambda x, e: x/(h_diag-(e-casscf.ah_level_shift))
    u = numpy.eye(nmo)

    if isinstance(dx, int):
        x0 = g_orb0
        g_orb = g_orb0
    else:
        x0 = dx
        g_orb = g_orb0 + h_op(dx)

    g_op = lambda: g_orb
    imic = 0
    for ihop, w, dxi in aug_hessian.davidson_cc(h_op, g_op, precond, x0, log, \
                                                tol=casscf.ah_conv_threshold, \
                                                toloose=1e-4,#casscf.conv_threshold_grad, \
                                                max_cycle=casscf.ah_max_cycle, \
                                                max_stepsize=1.5, \
                                                lindep=casscf.ah_lindep):
        imic += 1
        dx1 = dxi
        dxmax = numpy.max(abs(dx1))
        if dxmax > casscf.max_orb_stepsize:
            dx1 = dx1 * (casscf.max_orb_stepsize/dxmax)
        dx = dx + dx1
        dr = casscf.unpack_uniq_var(dx1)
        u = numpy.dot(u, expmat(dr))

        norm_gprev = numpy.linalg.norm(g_orb)
# within few steps, g_orb + \sum_i h_op(dx_i) is a good approximation to the
# exact gradients. After few updates, decreasing the approx gradients may
# result in the increase of the real gradient.
        g_orb = g_orb0 + h_op(dx)
        norm_gorb = numpy.linalg.norm(g_orb)
        norm_dx1 = numpy.linalg.norm(dx1)
        log.debug('    inner iter %d, |g[o]|=%4.3g, |dx|=%4.3g, max(|x|)=%4.3g, eig=%4.3g',
                   imic, norm_gorb, norm_dx1, dxmax, w)
        if imic >= casscf.max_cycle_micro_inner \
           or norm_gorb > norm_gprev \
           or norm_gorb < casscf.conv_threshold_grad:
            break

    t3m = log.timer('aug_hess in %d inner iters' % imic, *t3m)
    return u, dx, g_orb, imic+ihop

# dc = h_{co} * dr
def hessian_co(casscf, mo, rmat, fcivec, e_ci, eris):
    ncas = casscf.ncas
    nelecas = casscf.nelecas
    ncore = casscf.ncore
    nmo = mo.shape[1]
    nocc = ncore + ncas
    mocc = mo[:,:nocc]

    h1e_mo = reduce(numpy.dot, (mo.T, casscf.get_hcore(), mocc))
    h1eff = numpy.dot(rmat[:,:nocc].T, h1e_mo)
    h1eff = h1eff + h1eff.T

    apca = eris.appa[:,:,:ncore,:]
    aapc = eris.aapp[:,:,:,:ncore]
    jk = numpy.einsum('iup->up', eris.jc_pp[:,:nocc]) * 2 \
       - numpy.einsum('iup->up', eris.kc_pp[:,:nocc])
    v1 = numpy.einsum('up,pv->uv', jk[ncore:], rmat[:,ncore:nocc]) \
       + numpy.einsum('uvpi,pi->uv', aapc-apca.transpose(0,3,1,2)*.5,
                      rmat[:,:ncore]) * 2
    h1cas = h1eff[ncore:,ncore:] + (v1 + v1.T)

    aaap = eris.aapp[:,:,ncore:nocc,:]
    aaaa = numpy.einsum('tuvp,pw->tuvw', aaap, rmat[:,ncore:nocc])
    aaaa = aaaa + aaaa.transpose(0,1,3,2)
    aaaa = aaaa + aaaa.transpose(2,3,0,1)
    h2eff = casscf.fcisolver.absorb_h1e(h1cas, aaaa, ncas, nelecas, .5)
    hc = casscf.fcisolver.contract_2e(h2eff, fcivec, ncas, nelecas).ravel()

    # pure core response
    # J from [(i^1i|jj) + (ii^1|jj) + (ii|j^1j) + (ii|jj^1)] has factor 4
    # spin-free (ii| gives another 2*2, which provides a total factor 16
    # times 1/2 for 2e-integrals. So the J^1 part has factor 8, K^1 has 4
    ecore = h1eff[:ncore,:ncore].trace()*2 \
          + numpy.einsum('jp,pj->', jk[:ncore], rmat[:,:ncore])*4
    hc += ecore * fcivec.ravel()
    return hc

# dr = h_{oc} * dc
def hessian_oc(casscf, mo, dci, fcivec, eris):
    ncas = casscf.ncas
    nelecas = casscf.nelecas
    ncore = casscf.ncore
    nmo = mo.shape[1]
    nocc = ncore + ncas
    mocc = mo[:,:nocc]

    tdm1, tdm2 = casscf.fcisolver.trans_rdm12(dci, fcivec, ncas, nelecas)
    tdm1 = (tdm1 + tdm1.T)
    tdm2 = (tdm2 + tdm2.transpose(1,0,3,2))

    inner1 = numpy.dot(dci.flatten(),fcivec.flatten()) * 2

    dm1 = numpy.zeros((nmo,nmo))
    for i in range(ncore):
        dm1[i,i] = 2 * inner1 # p^+ q in core, factor due to <c|0> + <0|c>
    dm1[ncore:nocc,ncore:nocc] = tdm1

    vhf_c = numpy.einsum('ipq->pq', eris.jc_pp) * 2 \
          - numpy.einsum('ipq->pq', eris.kc_pp)
    vhf_a = numpy.einsum('uvpq,uv->pq', eris.aapp, tdm1) \
          - numpy.einsum('upqv,uv->pq', eris.appa, tdm1) * .5

    aaap = eris.aapp[:,:,ncore:nocc,:]
    g2dm = numpy.dot(aaap.reshape(-1, nmo).T, tdm2.reshape(-1,ncas))

    h1e_mo = reduce(numpy.dot, (mo.T, casscf.get_hcore(), mo))
    g = numpy.dot(h1e_mo, dm1)
    g[:,:ncore] += vhf_a[:,:ncore] * 2
    g[:,:ncore] += vhf_c[:,:ncore] *(2 * inner1)
    g[:,ncore:nocc] += g2dm + numpy.dot(vhf_c[:,ncore:nocc], tdm1)
    return casscf.pack_uniq_var(g - g.T)


def kernel(casscf, mo_coeff, tol=1e-7, macro=30, micro=8, \
           ci0=None, verbose=None, **cikwargs):
    if verbose is None:
        verbose = casscf.verbose
    log = logger.Logger(casscf.stdout, verbose)
    cput0 = (time.clock(), time.time())
    log.debug('Start 1-step CASSCF')

    mo = mo_coeff
    if isinstance(mo, numpy.ndarray) and mo.ndim == 2:
        _uhf_mo = False
        nmo = mo.shape[1]
    else:
        _uhf_mo = True
        nmo = mo[0].shape[1]
    #TODO: lazy evaluate eris, to leave enough memory for FCI solver
    eris = casscf.update_ao2mo(mo)
    e_tot, e_ci, fcivec = casscf.casci(mo, ci0, eris, **cikwargs)
    log.info('CASCI E = %.15g', e_tot)
    elast = e_tot
    conv = False
    toloose = casscf.conv_threshold_grad
    totmicro = totinner = 0
    imicro = 0
    norm_gorb = norm_gci = 0

    t2m = t1m = log.timer('Initializing 1-step CASSCF', *cput0)
    for imacro in range(macro):
        u, dx, g_orb, ninner = casscf.rotate_orb(mo, fcivec, e_ci, eris, 0)
        norm_gorb = numpy.linalg.norm(g_orb)
        t3m = log.timer('orbital rotation', *t2m)
        totmicro += 1
# dynamic ci_stepsize
# ci graidents may change the CI vector too much, which causes oscillation in
# macro iterations
        max_ci_stepsize = min(norm_gorb*50, casscf.max_ci_stepsize)

        for imicro in range(micro):

# approximate newton step, fcivec is not updated during micro iters
            #dr = casscf.unpack_uniq_var(dx)
            dr = u - numpy.eye(nmo)
            gci = casscf.hessian_co(mo, dr, fcivec, e_ci, eris)
            t3m = log.timer('ci gradient', *t3m)

# Perturbation updating   gci/(e_ci-hci_diag), gci = H^1 ci^0  is the way
# davidson.dsyev generate new trial vector.  Numerically,
# * Perturbation updating is worse than the simple gradeint.
# * Restorsing the davidson.dsyev hessian from previous FCI solver as the
#   approx CI hessian then solving  (H-E*1)dc = g or aug-hessian or H dc = g
#   has not obvious advantage than simple gradeint.
            dc = gci.ravel()
            dcmax = numpy.max(abs(dc))

            #norm_dc = numpy.linalg.norm(dc)
            #if norm_dc > casscf.max_ci_stepsize:
            #    dc = dc * (casscf.max_ci_stepsize/norm_dc)
            #if dcmax > casscf.max_ci_stepsize:
            #    ci1 = fcivec.ravel() - dc * (casscf.max_ci_stepsize/dcmax)
            #else:
            #    ci1 = fcivec.ravel() - dc
            if dcmax > max_ci_stepsize:
                ci1 = fcivec.ravel() - dc * (max_ci_stepsize/dcmax)
            else:
                ci1 = fcivec.ravel() - dc
            ci1 *= 1/numpy.linalg.norm(ci1)

            ovlp_ci = numpy.dot(ci1.ravel(), fcivec.ravel())
            norm_gci = numpy.linalg.norm(gci)

            u1, dx, g_orb, nin = casscf.rotate_orb(mo, ci1, e_ci, eris, dx)
            ci1 = None
            if _uhf_mo:
                u = map(numpy.dot, u, u1)
            else:
                u = numpy.dot(u, u1)
            ninner += nin
            t3m = log.timer('orbital rotation', *t3m)
            norm_dt = numpy.linalg.norm(u-numpy.eye(nmo))
            norm_gorb = numpy.linalg.norm(g_orb)
            totmicro += 1

            log.debug('micro %d, e_ci = %.12g, |u-1|=%4.3g, |g[o]|=%4.3g, ' \
                      '|g[c]|=%4.3g, max|dc|=%4.3g, <c|c+dc>=%.8g',
                      imicro, e_ci, norm_dt, norm_gorb, \
                      norm_gci, dcmax, ovlp_ci)
            t2m = log.timer('micro iter %d'%imicro, *t2m)
            if (norm_dt < toloose or norm_gorb < toloose or norm_gci < toloose) \
               or dcmax < toloose or 1-ovlp_ci < 1e-8:
                break

        totinner += ninner

        if _uhf_mo:
            mo = map(numpy.dot, mo, u)
        else:
            mo = numpy.dot(mo, u)
        casscf.save_mo_coeff(mo, imacro, imicro)

        eris = None # to avoid using too much memory
        eris = casscf.update_ao2mo(mo)
        t3m = log.timer('update eri', *t3m)

        e_tot, e_ci, fcivec = casscf.casci(mo, fcivec, eris, **cikwargs)
        log.info('macro iter %d (%d ah, %d micro), CASSCF E = %.15g, dE = %.8g,',
                 imacro, ninner, imicro+1, e_tot, e_tot-elast)
        norm_gorb = numpy.linalg.norm(g_orb)
        log.info('                        |grad[o]| = %6.5g, |grad[c]| = %6.5g',
                 norm_gorb, norm_gci)
        log.timer('CASCI solver', *t3m)
        t2m = t1m = log.timer('macro iter %d'%imacro, *t1m)

        if abs(e_tot - elast) < tol \
           and (norm_gorb < toloose and norm_gci < toloose):
            conv = True
            break
        else:
            elast = e_tot

    if conv:
        log.info('1-step CASSCF converged in %d macro (%d JK %d micro) steps',
                 imacro+1, totinner, totmicro)
    else:
        log.info('1-step CASSCF not converged, %d macro (%d JK %d micro) steps',
                 imacro+1, totinner, totmicro)
    log.log('1-step CASSCF, energy = %.15g', e_tot)
    log.timer('1-step CASSCF', *cput0)
    return e_tot, e_ci, fcivec, mo


# To extend CASSCF for certain CAS space solver, it can be done by assign an
# object or a module to CASSCF.fcisolver.  The fcisolver object or module
# should at least have three member functions "kernel" (wfn for given
# hamiltonain), "make_rdm12" (1- and 2-pdm), "absorb_h1e" (effective
# 2e-hamiltonain) in 1-step CASSCF solver, and two member functions "kernel"
# and "make_rdm12" in 2-step CASSCF solver
class CASSCF(casci.CASCI):
    def __init__(self, mol, mf, ncas, nelecas, ncore=None):
        casci.CASCI.__init__(self, mol, mf, ncas, nelecas, ncore)
# the max orbital rotation and CI increment, prefer small step size
        self.max_orb_stepsize = .03
# small max_ci_stepsize is good to converge, since steepest descent is used
        self.max_ci_stepsize = .01
#TODO:self.inner_rotation = False # active-active rotation
        self.max_cycle_macro = 50
        self.max_cycle_micro = 2
# num steps to approx orbital rotation without integral transformation.
# Increasing steps do not help converge since the approx gradient might be
# very diff to real gradient after few steps. If the step predicted by AH is
# good enough, it can be set to 1 or 2 steps.
        self.max_cycle_micro_inner = 4
        self.conv_threshold = 1e-7
        self.conv_threshold_grad = 1e-4
        # for augmented hessian
        self.ah_level_shift = 0#1e-2
        self.ah_conv_threshold = 1e-8
        self.ah_max_cycle = 15
        self.ah_lindep = self.ah_conv_threshold**2
        self.chkfile = mf.chkfile

        self.e_tot = None
        self.ci = None
        self.mo_coeff = mf.mo_coeff

        self._keys = set(self.__dict__.keys() + ['_keys'])

    def dump_flags(self):
        log = logger.Logger(self.stdout, self.verbose)
        log.info('')
        log.info('******** CASSCF flags ********')
        nvir = self.mo_coeff.shape[1] - self.ncore - self.ncas
        log.info('CAS (%de+%de, %do), ncore = %d, nvir = %d', \
                 self.nelecas[0], self.nelecas[1], self.ncas, self.ncore, nvir)
        log.info('max. macro cycles = %d', self.max_cycle_macro)
        log.info('max. micro cycles = %d', self.max_cycle_micro)
        log.info('conv_threshold = %g, (%g for gradients)', \
                 self.conv_threshold, self.conv_threshold_grad)
        log.info('max_cycle_micro_inner = %d', self.max_cycle_micro_inner)
        log.info('max. orb step = %g', self.max_orb_stepsize)
        log.info('max. ci step = %g', self.max_ci_stepsize)
        log.info('augmented hessian max. cycle = %d', self.ah_max_cycle)
        log.info('augmented hessian conv_threshold = %g', self.ah_conv_threshold)
        log.info('augmented hessian linear dependence = %g', self.ah_lindep)
        log.info('augmented hessian level shift = %d', self.ah_level_shift)
        log.info('max_memory %d MB', self.max_memory)
        try:
            self.fcisolver.dump_flags(self.verbose)
        except:
            pass

    def mc1step(self, mo=None, ci0=None, macro=None, micro=None, **cikwargs):
        if mo is None:
            mo = self.mo_coeff
        else:
            self.mo_coeff = mo
        if macro is None:
            macro = self.max_cycle_macro
        if micro is None:
            micro = self.max_cycle_micro

        self.mol.check_sanity(self)

        self.dump_flags()

        self.e_tot, e_cas, self.ci, self.mo_coeff = \
                kernel(self, mo, \
                       tol=self.conv_threshold, macro=macro, micro=micro, \
                       ci0=ci0, verbose=self.verbose, **cikwargs)
        return self.e_tot, e_cas, self.ci, self.mo_coeff

    def mc2step(self, mo=None, ci0=None, macro=None, micro=None, **cikwargs):
        import mc2step
        if mo is None:
            mo = self.mo_coeff
        else:
            self.mo_coeff = mo
        if macro is None:
            macro = self.max_cycle_macro
        if micro is None:
            micro = self.max_cycle_micro

        self.mol.check_sanity(self)

        self.dump_flags()

        self.e_tot, e_cas, self.ci, self.mo_coeff = \
                mc2step.kernel(self, mo, \
                               tol=self.conv_threshold, macro=macro, micro=micro, \
                               ci0=ci0, verbose=self.verbose, **cikwargs)
        return self.e_tot, e_cas, self.ci, self.mo_coeff

    def casci(self, mo, ci0=None, eris=None, **cikwargs):
        if eris is None:
            fcasci = self
        else:
            fcasci = _fake_h_for_fast_casci(self, mo, eris)
        return casci.kernel(fcasci, mo, ci0=ci0, verbose=0, **cikwargs)

    def pack_uniq_var(self, mat):
        ncore = self.ncore
        nocc = ncore + self.ncas

        v = []
        # active-core
        v.append(mat[ncore:nocc,:ncore].ravel())
        #TODO:if self.inner_rotation:
        #TODO:    # active-active
        #TODO:    v.append(mat[ncore:nocc,ncore:nocc].ravel())
        # virtual-core, virtual-active
        v.append(mat[nocc:,:nocc].ravel())
        return numpy.hstack(v)

    # to anti symmetric matrix
    def unpack_uniq_var(self, v):
        ncore = self.ncore
        ncas = self.ncas
        nocc = ncore + ncas
        #TODO:if self.inner_rotation:
        #TODO:    nvir = v.size / nocc - ncas
        #TODO:else:
        #TODO:    nvir = (v.size-ncore*ncas) / nocc
        nvir = (v.size-ncore*ncas) / nocc
        nmo = nocc + nvir

        mat = numpy.zeros((nmo,nmo))
        if ncore > 0:
            mat[ncore:nocc,:ncore] = v[:ncas*ncore].reshape(ncas,-1)
            mat[:ncore,ncore:nocc] = -mat[ncore:nocc,:ncore].T
        #TODO:if self.inner_rotation:
        #TODO:    mat[ncore:nocc,ncore:nocc] = v[ncas*ncore:ncas*nocc].reshape(ncas,-1)
        # virtual-core, virtual-active
        if nvir > 0:
            mat[nocc:,:nocc] = v[-nvir*nocc:].reshape(nvir,-1)
            mat[:nocc,nocc:] = -mat[nocc:,:nocc].T
        return mat

    def gen_g_hop(self, *args):
        return gen_g_hop(self, *args)

    def rotate_orb(self, mo, fcivec, e_ci, eris, dx=0):
        return rotate_orb_ah(self, mo, fcivec, e_ci, eris, dx, self.verbose)

    def update_ao2mo(self, mo):
#        nmo = mo.shape[1]
#        ncore = self.ncore
#        ncas = self.ncas
#        nocc = ncore + ncas
#        eri = pyscf.ao2mo.incore.full(self._scf._eri, mo)
#        eri = pyscf.ao2mo.restore(1, eri, nmo)
#        eris = lambda:None
#        eris.jc_pp = numpy.einsum('iipq->ipq', eri[:ncore,:ncore,:,:])
#        eris.kc_pp = numpy.einsum('ipqi->ipq', eri[:ncore,:,:,:ncore])
#        eris.aapp = numpy.array(eri[ncore:nocc,ncore:nocc,:,:])
#        eris.appa = numpy.array(eri[ncore:nocc,:,:,ncore:nocc])
## for update_jk_in_ah
#        capp = eri[:ncore,ncore:nocc,:,:]
#        cpap = eri[:ncore,:,ncore:nocc,:]
#        ccvp = eri[:ncore,:ncore,ncore:,:]
#        cpcv = eri[:ncore,:,:ncore,ncore:]
#        cvcp = eri[:ncore,ncore:,:ncore,:]
#        cPAp = cpap * 4 - capp.transpose(0,3,1,2) - cpap.transpose(0,3,2,1)
#        cPCv = cpcv * 4 - ccvp.transpose(0,3,1,2) - cvcp.transpose(0,3,2,1)
#        eris.Iapcv = cPAp.transpose(2,3,0,1)[:,:,:,ncore:]
#        eris.Icvcv = cPCv.transpose(2,3,0,1).copy()
#        return eris
        return mc_ao2mo._ERIS(self, mo)

    def update_jk_in_ah(self, mo, r, casdm1, eris):
# J3 = eri_popc * pc + eri_cppo * cp
# K3 = eri_ppco * pc + eri_pcpo * cp
# J4 = eri_pcpa * pa + eri_appc * ap
# K4 = eri_ppac * pa + eri_papc * ap
        ncore = self.ncore
        ncas = self.ncas
        nocc = ncore + ncas
        nmo = mo.shape[1]
        nvir = nmo - ncore
        vhf3c = numpy.dot(eris.Icvcv.reshape(-1,ncore*nvir),
                          r[:ncore,ncore:].ravel()).reshape(ncore,-1)
        vhf3a = numpy.einsum('uqcp,cp->uq', eris.Iapcv, r[:ncore,ncore:])
        dm4 = numpy.dot(casdm1, r[ncore:nocc])
        vhf4 = numpy.einsum('uqcp,uq->cp', eris.Iapcv, dm4)
        va = numpy.dot(casdm1, vhf3a)
        vc = 2 * vhf3c + vhf4
        return va, vc

    def hessian_co(self, *args):
        return hessian_co(self, *args)

    def save_mo_coeff(self, mo_coeff, *args):
        pyscf.scf.chkfile.dump(self.chkfile, 'mcscf/mo_coeff', mo_coeff)
    def load_mo_coeff(self):
        return pyscf.scf.chkfile.load(self.chkfile, 'mcscf/mo_coeff')


# to avoid calculating AO integrals
def _fake_h_for_fast_casci(casscf, mo, eris):
    mc = copy.copy(casscf)
    mc.mo_coeff = mo
    # vhf for core density matrix
    mo_inv = numpy.dot(mo.T, mc._scf.get_ovlp())
    vj = numpy.einsum('ipq->pq', eris.jc_pp)
    vk = numpy.einsum('ipq->pq', eris.kc_pp)
    vhf = reduce(numpy.dot, (mo_inv.T, vj*2-vk, mo_inv))
    mc.get_veff = lambda *args: vhf

    ncore = casscf.ncore
    nocc = ncore + casscf.ncas
    eri_cas = eris.aapp[:,:,ncore:nocc,ncore:nocc].copy()
    mc.ao2mo = lambda *args: eri_cas
    return mc


if __name__ == '__main__':
    from pyscf import gto
    from pyscf import scf
    import pyscf.fci
    import addons

    mol = gto.Mole()
    mol.verbose = 0
    mol.output = None#"out_h2o"
    mol.atom = [
        ['H', ( 1.,-1.    , 0.   )],
        ['H', ( 0.,-1.    ,-1.   )],
        ['H', ( 1.,-0.5   ,-1.   )],
        ['H', ( 0.,-0.5   ,-1.   )],
        ['H', ( 0.,-0.5   ,-0.   )],
        ['H', ( 0.,-0.    ,-1.   )],
        ['H', ( 1.,-0.5   , 0.   )],
        ['H', ( 0., 1.    , 1.   )],
    ]

    mol.basis = {'H': 'sto-3g',
                 'O': '6-31g',}
    mol.build()

    m = scf.RHF(mol)
    ehf = m.scf()
    emc = kernel(CASSCF(mol, m, 4, 4), m.mo_coeff, verbose=4)[0] + mol.nuclear_repulsion()
    print(ehf, emc, emc-ehf)
    print(emc - -3.22013929407)

    mc = CASSCF(mol, m, 4, (3,1))
    mc.verbose = 4
    #mc.fcisolver = pyscf.fci.direct_spin1
    mc.fcisolver = pyscf.fci.solver(mol, False)
    emc = kernel(mc, m.mo_coeff, verbose=4)[0]
    print(emc - -15.950852049859)


    mol.atom = [
        ['O', ( 0., 0.    , 0.   )],
        ['H', ( 0., -0.757, 0.587)],
        ['H', ( 0., 0.757 , 0.587)],]
    mol.basis = {'H': 'cc-pvdz',
                 'O': 'cc-pvdz',}
    mol.build()

    m = scf.RHF(mol)
    ehf = m.scf()
    mc = CASSCF(mol, m, 6, 4)
    mc.fcisolver = pyscf.fci.solver(mol)
    mc.verbose = 4
    mo = addons.sort_mo(mc, m.mo_coeff, (3,4,6,7,8,9), 1)
    emc = mc.mc1step(mo)[0] + mol.nuclear_repulsion()
    print(ehf, emc, emc-ehf)
    #-76.0267656731 -76.0873922924 -0.0606266193028
    print(emc - -76.0873923174, emc - -76.0926176464)

    mc = CASSCF(mol, m, 6, (3,1))
    #mc.fcisolver = pyscf.fci.direct_spin1
    mc.fcisolver = pyscf.fci.solver(mol, False)
    mc.verbose = 4
    emc = mc.mc1step(mo)[0]
    print(emc - -84.9038216713284)
