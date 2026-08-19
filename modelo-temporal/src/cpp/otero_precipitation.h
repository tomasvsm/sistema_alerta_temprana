#ifndef ModelH
#define ModelH

#include "types.h"
#include "configuration.h"
#include "parameters.h"
#include "weather.h"
#include "equations.h"
#include "rk.h"

class Model
{
    public:
    Parameters parameters;
    std::string start_date;
    std::string end_date;
    std::vector<scalar> time_range;
    std::vector<tensor> Y;

    Model():Model("resources/otero_precipitation.cfg"){}
    Model(const std::string& filename):Model(Configuration(filename)){}
    explicit Model(Configuration configuration){//explicit is just to avoid a cpp check warning
        std::setlocale(LC_NUMERIC,"C");//force default locale. More in "Strange error" in the readme.md

        this->parameters.BS_a=configuration.getScalar("breeding_site","amount");
        this->parameters.BS_lh=configuration.getScalar("breeding_site","level_height");//#in cm
        this->parameters.vBS_h=configuration.getTensor("breeding_site","height");//#in cm
        this->parameters.vBS_r=configuration.getTensor("breeding_site","radius");//#in cm
        this->parameters.vBS_s=configuration.getTensor("breeding_site","surface");//#in cm^2
        this->parameters.vBS_d=configuration.getTensor("breeding_site","distribution");//#distribution of BS. Sum must be equals to 1
        this->parameters.vBS_mf=configuration.getTensor("breeding_site","manually_filled");//#in cm
        this->parameters.vBS_b=configuration.getTensor("breeding_site","bare");//#in [0,1]
        this->parameters.vBS_ef=configuration.getTensor("breeding_site","evaporation_factor");//#in [0,2]
        this->parameters.n=this->parameters.vBS_d.size();
        this->parameters.m=ceil((this->parameters.vBS_h/this->parameters.BS_lh).maxCoeff());////<---- this is implemented a bit different in python


        unsigned int m=this->parameters.m;unsigned int n=this->parameters.n;
        this->parameters.EGG=Eigen::seqN(0,m*n);//#in R^(mxn)
        this->parameters.LARVAE=Eigen::seqN(m*n,n);//#in R^n
        this->parameters.PUPAE=Eigen::seqN((1+m)*n,n);//#in R^n
        this->parameters.ADULT1=(2+m)*n;//#in R
        this->parameters.ADULT2=(2+m)*n+1;//#in R
        this->parameters.WATER=Eigen::seqN((2+m)*n+2,n);//#in R^n
        this->parameters.OVIPOSITION=Eigen::seqN((3+m)*n+2,m*n);//#in R^(mxn)

        this->parameters.vAlpha0=configuration.getTensor("biology","alpha0");//#constant to be fitted

        this->parameters.location=configuration.get("location","name");
        this->start_date=configuration.get("simulation","start_date");
        this->end_date=configuration.get("simulation","end_date");
        this->parameters.mBS_l=matrix(m,n);//m rows x n columns. access by M[row][column]//#level helper matrix
        for(unsigned int i=0;i<m;i++) for(unsigned int j=0;j<n;j++)  this->parameters.mBS_l(i,j)=i;
        tensor initial_condition=configuration.getTensor("simulation","initial_condition");
        matrix mE0=matrix(m,n);//m rows x n column
        mE0(0,Eigen::indexing::all)= initial_condition(0)*this->parameters.vBS_d;
        tensor E0=mE0.transpose().reshaped(1,m*n).eval();//https://eigen.tuxfamily.org/dox-devel/group__TutorialReshape.html//Utils::matrixToTensor(mE0);
        tensor L0=initial_condition[1]*this->parameters.vBS_d;
        tensor P0=initial_condition[2]*this->parameters.vBS_d;
        tensor A0=initial_condition(Eigen::seqN(3,2));//index change, bug in previos code?
        tensor W0=configuration.getTensor("breeding_site","initial_water");
        tensor O0=tensor(m*n);
        this->parameters.initial_condition=tensor(E0.cols()+L0.cols()+P0.cols()+2+W0.cols()+O0.cols());
        this->parameters.initial_condition<<E0,L0,P0,A0,W0,O0;

        std::string WEATHER_DATA_FILENAME="data/public/"+this->parameters.location+".csv";
        this->parameters.weather=Weather(WEATHER_DATA_FILENAME, this->start_date ,this->end_date );
        unsigned int days=Utils::getDaysFromCsv(WEATHER_DATA_FILENAME, this->start_date ,this->end_date );
        for(unsigned int i=0;i<days;i++) this->time_range.push_back(i);


        this->parameters.mf=[](scalar t) { return (1.-std::min(int(t)%7,1))* (sin(2.*M_PI*t + 3.*M_PI/2.) +1.); };//<---- this is implemented different in python
    }


    std::tuple<std::vector<scalar>,std::vector<tensor>> solveEquations(){
        std::vector<scalar> time_range= this->time_range;
        tensor Y0=this->parameters.initial_condition;
        std::vector<tensor> Y=RK::solve(diff_eqs,Y0,time_range,this->parameters,20);
        this->Y=Y;
        return std::tie(this->time_range,this->Y);
    }

};

#endif
