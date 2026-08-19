#ifndef RKH
#define RKH

#include <vector>
#include "types.h"

class RK{
  public:
    static std::vector<tensor> solve(tensor (*dYdt)(const tensor&, scalar, scalar, Parameters&),tensor& Y0,std::vector<scalar>& time_range,Parameters& parameters, int steps){//TODO:use c++ functor instead
        //main
        std::vector<tensor> Y=std::vector<tensor>();
        Y.reserve(time_range.size());//small performance gain
        Y.push_back(Y0);//Y[0]=Y0<---initial conditions

        tensor Y_j=Y0;
        for(unsigned int i=0; i<time_range.size()-1;i++){
            scalar t=time_range[i];
            scalar h=time_range[i+1]-time_range[i];
            scalar h_j=h/scalar(steps);
            for(int j=0;j<steps;j++){
                //#Runge-Kutta's terms
                tensor K_n1=dYdt(Y_j,t,h_j, parameters);
                tensor K_n2=dYdt(Y_j + (h_j/2.)*K_n1, t + h_j/2., h_j, parameters);
                tensor K_n3=dYdt(Y_j + (h_j/2.)*K_n2, t + h_j/2., h_j, parameters);
                tensor K_n4=dYdt(Y_j + h_j*K_n3, t + h_j, h_j, parameters);

                Y_j = Y_j + (h_j/6.0)*(K_n1 + 2.0*K_n2 + 2.0*K_n3 + K_n4);
                t=t+h_j;
            }
            Y.push_back(Y_j);
        }

        return Y;
    }
};

#endif
