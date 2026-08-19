import numpy as np
import pylab as pl
import math

#"Elementary Differential Equations and Boundary Value Problem"Boyce, DiPrima:
def solve(dYdt,Y0,time_range,args=(),steps=1):
    #main
    Y=np.zeros([len(time_range),len(Y0)],dtype=np.float32)
    Y[0]=Y0#<---initial conditions

    for i,t in enumerate(time_range[:-1]):
        h=time_range[i+1]-time_range[i]
        Y_j=Y[i]
        h_j=h/float(steps)
        for j in range(0,steps):
            #Runge-Kutta's terms
            K_n1=dYdt(Y_j,t,h_j,*args)
            K_n2=dYdt(Y_j + (h_j/2.)*K_n1, t + h_j/2.,h_j,*args)
            K_n3=dYdt(Y_j + (h_j/2.)*K_n2, t + h_j/2.,h_j,*args)
            K_n4=dYdt(Y_j + h_j*K_n3, t + h_j,h_j,*args)

            Y_j=Y_j+ (h_j/6.0)*(K_n1 + 2.0*K_n2 + 2.0*K_n3 + K_n4)
            t=t+h_j
        Y[i+1]=Y_j

    return Y

try:
    import cupy as cp
except ImportError:
    pass

#this is way too similar to the RK method above
def cuda_solve(_dYdt,Y0,time_range,args=(),steps=1,dtype=np.float32):
    #numpy array ----> cupy arrays
    Y0=cp.array(Y0,dtype=dtype)
    parameters,=args
    for param_name in parameters.__dict__:
        if(isinstance(parameters.__dict__[param_name],np.ndarray)):
            parameters.__dict__[param_name]=cp.array(parameters.__dict__[param_name],dtype=dtype)

    #main
    Y=np.memmap('out/Y.npy.swap', mode='w+', shape=(len(time_range),len(Y0)), dtype=dtype)
    Y[0]=Y0.get()#<---initial conditions
    def dYdt(Y,t,_args):
        Y[Y<0]=0#this is to make rk work
        return _dYdt(Y,t,*_args)

    Y_j=Y0
    for i,t in enumerate(time_range[:-1]):
        h=time_range[i+1]-time_range[i]
        h_j=h/float(steps)
        for j in range(0,steps):
            #Runge-Kutta's terms
            K_n1=dYdt(Y_j,t,args)
            K_n2=dYdt(Y_j + (h_j/2.)*K_n1, t + h_j/2.,args)
            K_n3=dYdt(Y_j + (h_j/2.)*K_n2, t + h_j/2.,args)
            K_n4=dYdt(Y_j + h_j*K_n3, t + h_j,args)

            Y_j=Y_j+ (h_j/6.0)*(K_n1 + 2.0*K_n2 + 2.0*K_n3 + K_n4)
            t=t+h_j
        Y[i+1]=Y_j.get()#use stream to make this async, https://docs-cupy.chainer.org/en/stable/reference/generated/cupy.ndarray.html

    return Y


d_21=1/4
d_31,d_32=3/32, 9/32
d_41,d_42,d_43=1932/2197, -7200/2197, 7296/2197
d_51,d_52,d_53,d_54=439/216, -8, 3680/513, -845/4104
d_61,d_62,d_63,d_64,d_65=-8/27, 2, -3544/2565, 1859/4104, -11/40
c_2,c_3,c_4,c_5,c_6=1/4, 3/8, 12/13, 1, 1/2
a_1,a_2,a_3,a_4,a_5,a_6=16/135, 0, 6656/12825, 28561/56430, -9/50, 2/55
a1_b1,a2_b2,a3_b3,a4_b4,a5_b5,a6_b6=1/360, 0, -128/4275, -2197/75240, 1/50, 2/55
#Numerical Analysis, TENTH EDITION - Richard L. Burden, J. Douglas Faires and Annette M. Burden #<----- base
#NUMERICAL ANALYSIS MATHEMATICS OF SCIENTIFIC COMPUTING - David Kincaid and Ward Cheney
#Numerical Methods Using Matlab, 4 th Edition, 2004 - John H. Mathews and Kurtis K. Fink
#http://www.math-cs.gordon.edu/courses/ma342/python/diffeq.py
from numpy.core.multiarray import interp as compiled_interp
def rkf_solve(dYdt,Y0,time_range,args=(),tol=50.,hmin=0.001,hmax=0.05):
    #main
    a=time_range.min()
    b=time_range.max()
    Y_j=Y0#<---initial conditions
    t=a
    h=hmax
    Y=np.array([Y_j])
    T=np.array([t])
    while t<b:
        if t+h>b:
            h=b-t
        #Runge-Kutta-Fehlberg's terms
        F_1=h*dYdt(Y_j,t, *args)
        F_2=h*dYdt(Y_j +d_21*F_1, t + c_2*h, *args)
        F_3=h*dYdt(Y_j +d_31*F_1 + d_32*F_2, t + c_3*h, *args)
        F_4=h*dYdt(Y_j +d_41*F_1 + d_42*F_2 + d_43*F_3, t + c_4*h, *args)
        F_5=h*dYdt(Y_j +d_51*F_1 + d_52*F_2 + d_53*F_3 + d_54*F_4, t + c_5*h, *args)
        F_6=h*dYdt(Y_j +d_61*F_1 + d_62*F_2 + d_63*F_3 + d_64*F_4 + d_65*F_5, t + c_6*h, *args)

        #compute error
        e= np.abs(a1_b1*F_1 + a3_b3*F_3 + a4_b4*F_4 + a5_b5*F_5 + a6_b6*F_6)/h
        e=np.max(e)
        if(e<=tol):
            t=t+h
            #compute rk of fifth order
            Y_j =Y_j + a_1*F_1 + a_2*F_2 + a_3*F_3 + a_4*F_4 + a_5*F_5 + a_6*F_6
            T=np.append(T,t)
            Y=np.append(Y,[Y_j],axis=0)

        #compute new step
        s=0.84*(tol/e)**(1/4)
        h=h* min(max(s, 0.1), 4.0)#h=h*s', force s' in [0.1,4]
        if(h>hmax):
            h=hmax
        elif (h<hmin):
            print('FATAL: step size should be smaller than %s, h needed:%s, reached t:%s'%(hmin,h,t))#TODO:append this to model.warnings
            break

    #https://stackoverflow.com/questions/43772218/fastest-way-to-use-numpy-interp-on-a-2-d-array
    return np.concatenate([ np.array([compiled_interp(time_range, T, Y[:,i])]).transpose() for i in range(Y.shape[1])],axis=1 )

from scipy.integrate import ode
def scipy_solve(_dYdt,Y0,time_range,name,kwargs,args=()):
    def dYdt(t,Y): return np.array(_dYdt(Y,t,*args))#decorate the function to return an np array and swap args.
    r = ode(dYdt).set_integrator(name,**kwargs)
    r.set_initial_value(Y0, time_range[0])
    Y=np.zeros([len(time_range),len(Y0)])

    for i,t in enumerate(time_range[:-1]):
        h=time_range[i+1]-time_range[i]
        Y[i+1]=r.integrate(r.t+h)
        if(not r.successful):
            break

    return Y

def diff_eqs(Y,t):
	b = 0.25
	c = 5.0
	theta, omega = Y
	dydt = [omega, -b*omega - c*np.sin(theta)]
	return dydt

if (__name__ == '__main__'):
	Y0 = [np.pi - 0.1, 0.0]
	time_range=np.linspace(0, 10, 10*8)
	RES=solve(diff_eqs,Y0,time_range)

	pl.plot(RES[:,0])
	pl.plot(RES[:,1])
	pl.ylabel('y')
	pl.xlabel('t')
	pl.show()
