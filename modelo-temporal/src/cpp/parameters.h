#ifndef ParametersH
#define ParametersH

#include <functional>
#include "types.h"
#include "weather.h"

struct Parameters{
    //config
    scalar BS_a;
    scalar BS_lh;
    tensor vBS_d;
    tensor vBS_s;
    tensor vBS_h;
    tensor vBS_r;
    tensor vBS_W0;
    tensor vBS_mf;
    tensor vBS_b;
    tensor vBS_ef;
    matrix mBS_l;

    std::string location;

    std::string start_date;
    std::string end_date;
    tensor initial_condition;

    tensor vAlpha0;
    //dynamic
    unsigned int m;
    unsigned int n;
    Eigen::ArithmeticSequence<long int, long int> EGG=Eigen::seq(0,0);
    Eigen::ArithmeticSequence<long int, long int> LARVAE=Eigen::seq(0,0);
    Eigen::ArithmeticSequence<long int, long int> PUPAE=Eigen::seq(0,0);
    unsigned int ADULT1;
    unsigned int ADULT2;
    Eigen::ArithmeticSequence<long int, long int> WATER=Eigen::seq(0,0);
    Eigen::ArithmeticSequence<long int, long int> OVIPOSITION=Eigen::seq(0,0);
    Weather weather;
    std::function<scalar(scalar)> mf;

};

#endif
