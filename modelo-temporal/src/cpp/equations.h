#ifndef EquationsH
#define EquationsH

//TODO:make this a class?
#include "types.h"
#include "parameters.h"
#include "utils.h"



tensor vR_D_298K=(tensor(5)<< 0.24,0.2088,0.384,0.216,0.372).finished();//https://www.alecjacobson.com/weblog/?p=4207
//#ro_25_=[0.01066,0.00873,0.01610,0.00898] #replaced by R_D_298K. which:  R_D_298K ~ ro_25*24  #(24 hours)
tensor vDeltaH_A=(tensor(5)<< 10798.0,26018.0,14931.0,15725.0,15725.0).finished();
tensor vDeltaH_H=(tensor(5)<< 100000.0,55990.0,-472379.00,1756481.0,1756481.0).finished();// #-472379 vs. -473379
tensor vT_1_2H=(tensor(5)<< 14184.0,304.6,148.0,447.2,447.2).finished();

//<precipitation related functionality>
tensor dW(tensor& vW, tensor& vBS_h, tensor& vBS_ef, scalar T_t, tensor p_t, scalar RH_t, scalar h){//#in cm/day
    tensor QG_t=p_t*0.1;//#cm/day
    scalar QR_t=6e-5* std::pow(25 + T_t-273.15, 2) * (100.-RH_t) * 0.1;//#cm/day. #Ivanov
    tensor dW_t=QG_t- vBS_ef*QR_t;
    return Utils::minimum(Utils::maximum(dW_t,-vW/h),(vBS_h-vW)/h);
}

scalar a0(scalar W,scalar BS_r){
    return 70.0* W * M_PI*std::pow(BS_r,2) * 1e-3;//cm3 -> litres
}

scalar gamma(scalar L, scalar BS, scalar W,scalar BS_r);
tensor vGamma(const tensor& vL, tensor vBS_a, const tensor& vW, const tensor& vBS_r){
    tensor vGamma_t=tensor(vW.size());
    for(unsigned int i=0;i<vL.size();i++) vGamma_t[i]=gamma(vL[i],vBS_a[i],vW[i],vBS_r[i]);
    return vGamma_t;
}
//</precipitation related functionality v>

tensor vR_D(scalar T_t){//#day^-1
    scalar R=1.987; //# universal gas constant
    return vR_D_298K * (T_t/298.0) * Eigen::exp( (vDeltaH_A/R)* ((1.0/298.0)- (1.0/T_t)) ) / ( 1.0+ Eigen::exp( (vDeltaH_H/R)* ((1.0/vT_1_2H)-(1.0/T_t)) ) );
}

scalar gamma(scalar L, scalar BS, scalar W,scalar BS_r){
    scalar epsilon=1e-4;
    if(BS==0 || W <epsilon)//#W *1000./BS_s <0.1
        return 1.0;//#no water total inhibition#1960 Aedes aegypti (L.), The Yellow Fever Mosquito(Page 165)
    if(L/BS<=a0(W,BS_r)-epsilon)
        return  0;
    else if(a0(W,BS_r)-epsilon < L/BS && L/BS<a0(W,BS_r)+epsilon){
        //#a (a0-e) + b=0 => b=-a (a0 -e)
        //#a (a0 + e) + b=0.63 => a(a0+e) - a(a0-e) = 2 a e = 0.63 =>a=0.63/(2 e)
        scalar a=0.63/(2.0*epsilon);
        scalar b=-a*(a0(W,BS_r)-epsilon);
        return a * (L/BS) + b;
    }
    else// if(L/BS>=a0(W)+epsilon)//commented out to avoid warning(should have no effect)
        return 0.63;
}

matrix ovsp(const tensor& vW,const tensor& vBS_d,const tensor& vW_l,const matrix& mBS_l){
    scalar epsilon=1e-4;
    tensor vf=vW/(vW+epsilon) * vBS_d;
    vf=vf/std::max(vf.sum(),epsilon);
    return (mBS_l==vW_l.floor().replicate(mBS_l.rows(),1)).select(vf.replicate(mBS_l.rows(),1),0.);//TODO:find a way to write this simpler
}

matrix wetMask(const tensor& vW_l, const matrix& mBS_l){
    return (mBS_l<=vW_l.replicate(mBS_l.rows(),1)).cast<scalar>();//this cast seems obscure.https://stackoverflow.com/questions/50009258/how-to-do-element-wise-comparison-with-eigen
}

matrix dmE(const matrix& mE,const tensor& vL,scalar A1,scalar A2,const tensor& vW_t,const tensor& vBS_r, scalar BS_a,const tensor&  vBS_d,scalar elr,scalar ovr1, scalar ovr2,const matrix& wet_mask,const tensor& vW_l,const matrix& mBS_l){
    scalar egn=63.0;
    scalar me=0.01;//#mortality of the egg, for T in [278,303]
    matrix ovsp_t=ovsp(vW_t,vBS_d,vW_l,mBS_l);
    return egn  *( ovr1 *A1  + ovr2* A2)*ovsp_t - me * mE - elr*(1.-vGamma(vL,BS_a*vBS_d,vW_t,vBS_r)).replicate(mE.rows(),1)* mE*wet_mask;
}

tensor dvL(const matrix& mE,const tensor& vL,const tensor& vW,const tensor& vBS_r,scalar T_t,scalar BS_a,const tensor& vBS_d,scalar elr,scalar lpr,const tensor& vAlpha0,const matrix& wet_mask){
    scalar ml=0.01 + 0.9725 * std::exp(-(T_t-278.0)/2.7035);//#mortality of the larvae, for T in [278,303]
    scalar mdl=2.;//#mortality of dry larvae.TODO:Unjustified!
    tensor vAlpha=vAlpha0/(BS_a*vBS_d);
    scalar epsilon=1e-4;
    return elr* (1.-vGamma(vL,BS_a*vBS_d,vW,vBS_r)) * Utils::sumAxis0(mE*wet_mask) - ml*vL - vAlpha* vL*vL - lpr *vL - mdl*(1.- vW/(vW+epsilon))*vL;
}

tensor dvP(const tensor& vL,const tensor& vP,scalar T_t, scalar lpr,scalar par){
    scalar mp=0.01 + 0.9725 * std::exp(-(T_t-278.0)/2.7035);//#death of pupae
    return lpr*vL - mp*vP  - par*vP;
}

scalar dA1(const tensor& vP,scalar A1, scalar par,scalar ovr1){
    scalar ef=0.83;//#emergence factor
    scalar ma=0.09;//#for T in [278,303]
    return (par*ef*vP/2.0).sum() - ma*A1 - ovr1*A1;
}

scalar dA2(scalar A1,scalar A2,scalar ovr1){
    scalar ma=0.09;//#for T in [278,303]
    return ovr1*A1 - ma*A2;
}

matrix dmO(scalar A1,scalar A2,const tensor& vW_t, const tensor&  vBS_d,scalar ovr1, scalar ovr2, const tensor& vW_l,const matrix& mBS_l){
    scalar egn=63.0;
    matrix ovsp_t=ovsp(vW_t,vBS_d,vW_l,mBS_l);
    return egn*( ovr1 *A1  + ovr2* A2)*ovsp_t;
}


tensor diff_eqs(const tensor& Y,scalar t,scalar h,Parameters& parameters){
    scalar T_t=parameters.weather.T(t);
    scalar p_t=parameters.weather.p(t);
    scalar RH_t=parameters.weather.RH(t);
    tensor vR_D_t=vR_D(T_t);//https://stackoverflow.com/questions/37876288/is-there-a-one-liner-to-unpack-tuple-pair-into-references
    scalar elr=vR_D_t(0);
    scalar lpr=vR_D_t(1);
    scalar par=vR_D_t(2);
    scalar ovr1=vR_D_t(3);
    scalar ovr2=vR_D_t(4);


    scalar BS_a=parameters.BS_a;
    tensor vBS_h=parameters.vBS_h;
    tensor vBS_r=parameters.vBS_r;
    scalar BS_lh=parameters.BS_lh;
    tensor vBS_d=parameters.vBS_d;
    tensor vBS_b=parameters.vBS_b;
    tensor vBS_ef=parameters.vBS_ef;
    tensor vAlpha0=parameters.vAlpha0;
    unsigned int m=parameters.m;
    unsigned int n=parameters.n;
    matrix mBS_l=parameters.mBS_l;

    matrix mE=Y(parameters.EGG).reshaped(m,n);
    tensor vL=Y(parameters.LARVAE);
    tensor vP=Y(parameters.PUPAE);
    scalar A1=Y(parameters.ADULT1);
    scalar A2=Y(parameters.ADULT2);
    tensor vW=Y(parameters.WATER);
    tensor vW_l=vW/BS_lh;
    matrix wet_mask=wetMask(vW_l,mBS_l);
    tensor vmf_t=parameters.mf(t)*parameters.vBS_mf*10.;//# cm -> mm

    tensor dY=tensor(Y.size());
    dY(parameters.EGG)    = dmE(mE,vL,A1,A2,vW,vBS_r,BS_a,vBS_d,elr,ovr1,ovr2,wet_mask,vW_l,mBS_l).reshaped(1,m*n);
    dY(parameters.LARVAE) = dvL(mE,vL,vW,vBS_r,T_t,      BS_a,vBS_d,elr,lpr,vAlpha0,wet_mask);
    dY(parameters.PUPAE)  = dvP(vL,vP,T_t,lpr,par);
    dY(parameters.ADULT1) = dA1(vP,A1,par,ovr1);
    dY(parameters.ADULT2) = dA2(A1,A2,ovr1);
    dY(parameters.WATER) = dW(vW,vBS_h,vBS_ef,T_t,vBS_b*p_t+vmf_t,RH_t,h);
    dY(parameters.OVIPOSITION) = dmO(A1,A2,vW,vBS_d,ovr1,ovr2,vW_l,mBS_l).reshaped(1,m*n);

    return dY;//   # For odeint
}

#endif
