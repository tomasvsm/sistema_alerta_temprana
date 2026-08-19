#coding: utf-8
import numpy as np
import math

vR_D_298K=np.array([0.24,0.2088,0.384,0.216,0.372])
#ro_25_=[0.01066,0.00873,0.01610,0.00898] #replaced by R_D_298K. which:  R_D_298K ~ ro_25*24  #(24 hours)
vDeltaH_A=np.array([10798.0,26018.0,14931.0,15725.0,15725.0])
vDeltaH_H=np.array([100000.0,55990.0,-472379.00,1756481.0,1756481.0]) #-472379 vs. -473379
vT_1_2H=np.array([14184.0,304.6,148.0,447.2,447.2])


#<precipitation related functionality v>
def dW(vW,vBS_h,vBS_ef,T_t,p_t,RH_t,h):#in cm/day
    QG_t=p_t*0.1#cm/day
    QR_t=6e-5*(25 + T_t-273.15)**2 * (100.-RH_t) * 0.1#cm/day. #Ivanov
    dW_t=QG_t- vBS_ef*QR_t
    return np.minimum(np.maximum(dW_t,-vW/h),(vBS_h-vW)/h)

def a0(W,vBS_r):
    return 70.0* W * math.pi*vBS_r**2 * 1e-3

def vGamma(vL,vBS_a,vW,vBS_r):
    return np.array([gamma(vL[i],vBS_a[i],vW[i],vBS_r[i]) for i in range(0,len(vL))])

#</precipitation related functionality v>


def vR_D(T_t):#day^-1
    R=1.987 # universal gas constant
    return vR_D_298K * (T_t/298.0) * np.exp( (vDeltaH_A/R)* ((1.0/298.0)- (1.0/T_t)) ) / ( 1.0+ np.exp( (vDeltaH_H/R)* ((1.0/vT_1_2H)-(1.0/T_t)) ) )


def gamma(L,BS,W,vBS_r):
    epsilon=1e-4
    if(BS==0 or W <epsilon):#W *1000./BS_s <0.1
        return 1.0#no water total inhibition#1960 Aedes aegypti (L.), The Yellow Fever Mosquito(Page 165)
    if(L/BS<=a0(W,vBS_r)-epsilon):
        return 0
    elif(a0(W,vBS_r)-epsilon < L/BS <a0(W,vBS_r)+epsilon):
        #a (a0-e) + b=0 => b=-a (a0 -e)
        #a (a0 + e) + b=0.63 => a(a0+e) - a(a0-e) = 2 a e = 0.63 =>a=0.63/(2 e)
        a=0.63/(2.0*epsilon)
        b=-a*(a0(W,vBS_r)-epsilon)
        return a * (L/BS) + b
    elif(L/BS>=a0(W,vBS_r)+epsilon):
        return 0.63


def ovsp(vW,vBS_d,vW_l,mBS_l):#OViposition Site Preference
    epsilon=1e-4
    vf=vW/(vW+epsilon) * vBS_d#check this method is not spontaneus generation of eggs.(like inventing adults.)
    vf=vf/max(vf.sum(),epsilon)
    return np.where(mBS_l==np.floor(vW_l),1,0)*vf

def wetMask(vW_l,mBS_l):
    return np.where(mBS_l<=vW_l,1,0)

def dmE(mE,vL,A1,A2,vW_t,vBS_r,BS_a,vBS_d,elr,ovr1,ovr2,wet_mask,vW_l,mBS_l):
    egn=63.0
    me=0.01#mortality of the egg, for T in [278,303]
    ovsp_t=ovsp(vW_t,vBS_d,vW_l,mBS_l)
    return egn*( ovr1 *A1  + ovr2* A2)*ovsp_t - me * mE - elr* (1-vGamma(vL,BS_a*vBS_d,vW_t,vBS_r)) * mE*wet_mask

def dvL(mE,vL,vW,vBS_r,T_t,BS_a,vBS_d,elr,lpr,vAlpha0,wet_mask):
    ml=0.01 + 0.9725 * math.exp(-(T_t-278.0)/2.7035)#mortality of the larvae, for T in [278,303]
    epsilon=1e-4
    mdl=2.*(1.- vW/(vW+epsilon))#mortality of dry larvae.TODO:Unjustified!
    vAlpha=vAlpha0/(BS_a*vBS_d)
    return elr* (1-vGamma(vL,BS_a*vBS_d,vW,vBS_r)) * np.sum(mE*wet_mask,axis=0) - ml*vL - mdl*vL - vAlpha* vL*vL - lpr *vL

def dvP(vL,vP,T_t,lpr,par):
    mp=0.01 + 0.9725 * math.exp(-(T_t-278.0)/2.7035)#death of pupae
    return lpr*vL - mp*vP  - par*vP

def dA1(vP,A1,par,ovr1):
    ef=0.83#emergence factor
    ma=0.09#for T in [278,303]
    return np.sum(par*ef*vP/2.0) - ma*A1 - ovr1*A1

def dA2(A1,A2,ovr1):
    ma=0.09#for T in [278,303]
    return ovr1*A1 - ma*A2

def dmO(A1,A2,vW_t,vBS_d,ovr1,ovr2,vW_l,mBS_l):
    egn=63.0
    me=0.01#mortality of the egg, for T in [278,303]
    ovsp_t=ovsp(vW_t,vBS_d,vW_l,mBS_l)
    return egn*( ovr1 *A1  + ovr2* A2)*ovsp_t


def diff_eqs(Y,t,h,parameters):
    '''The main set of equations'''
    T_t=parameters.weather.T(t)
    p_t=parameters.weather.p(t)
    RH_t=parameters.weather.RH(t)
    elr,lpr,par,ovr1,ovr2=vR_D(T_t)
    BS_a,BS_lh,vBS_d,vBS_h,vBS_r,vBS_b,vBS_ef,vAlpha0,m,n,mBS_l=parameters.BS_a,parameters.BS_lh,parameters.vBS_d,parameters.vBS_h,parameters.vBS_r,parameters.vBS_b,parameters.vBS_ef,parameters.vAlpha0,parameters.m,parameters.n,parameters.mBS_l
    EGG,LARVAE,PUPAE,ADULT1,ADULT2,WATER,OVIPOSITION=parameters.EGG,parameters.LARVAE,parameters.PUPAE,parameters.ADULT1,parameters.ADULT2,parameters.WATER,parameters.OVIPOSITION

    vE,vL,vP,A1,A2,vW=Y[EGG].reshape((n,m)).transpose(),Y[LARVAE],Y[PUPAE],Y[ADULT1],Y[ADULT2],Y[WATER]
    vW_l=vW/BS_lh
    wet_mask=wetMask(vW_l,mBS_l)
    vmf_t=parameters.mf(t)*parameters.vBS_mf*10.# cm -> mm

    dY=np.empty(Y.shape)
    dY[EGG]    = dmE(vE,vL,A1,A2,vW,vBS_r,BS_a,vBS_d,elr,ovr1,ovr2,wet_mask,vW_l,mBS_l).transpose().reshape((1,m*n))
    dY[LARVAE] = dvL(vE,vL,vW,vBS_r,T_t,BS_a,vBS_d,elr,lpr,vAlpha0,wet_mask)
    dY[PUPAE]  = dvP(vL,vP,T_t,lpr,par)
    dY[ADULT1] = dA1(vP,A1,par,ovr1)
    dY[ADULT2] = dA2(A1,A2,ovr1)
    dY[WATER] = dW(vW,vBS_h,vBS_ef,T_t,vBS_b*p_t+vmf_t,RH_t,h)
    dY[OVIPOSITION]    = dmO(A1,A2,vW,vBS_d,ovr1,ovr2,vW_l,mBS_l).transpose().reshape((1,m*n))

    return dY   # For odeint
