//g++ -std=c++17 -Wall -O3 -march=native -shared -fPIC -I/usr/include/python3.8 src/cpp/otero_precipitation_wrapper.cpp -o src/otero_precipitation_wrapper.so
#include "otero_precipitation.h"
#include "configuration.h"
//order is important! if I put the pybind includes before mine, the result contains nans or infs.(The problem seems to be just with the eigen import)
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/functional.h>
#include <pybind11/eigen.h>

//Note: The important part is at the end (pybind11::class_<Model>), all other stuff is to convert "model.parameters"
PYBIND11_MODULE(otero_precipitation_wrapper, m) {

  pybind11::class_<Weather>(m, "Weather")
      .def(pybind11::init<>())
      .def_readonly("p", &Weather::p)//https://github.com/pybind/pybind11/issues/1275
      .def_readonly("T", &Weather::T)
      .def_readonly("RH", &Weather::RH);

  pybind11::class_<Eigen::ArithmeticSequence<long int, long int>>(m, "seq")
      .def(pybind11::init<long int, long int>())
      .def("first",&Eigen::ArithmeticSequence<long int, long int>::first)
      .def("size",&Eigen::ArithmeticSequence<long int, long int>::size);

  //https://pybind11.readthedocs.io/en/stable/classes.html#enumerations-and-internal-types
  pybind11::class_<Parameters>(m, "Parameters")
      .def(pybind11::init<>())
      .def_readonly("BS_a", &Parameters::BS_a)
      .def_readonly("BS_lh", &Parameters::BS_lh)
      .def_readonly("vBS_d", &Parameters::vBS_d)
      .def_readonly("vBS_s", &Parameters::vBS_s)
      .def_readonly("vBS_h", &Parameters::vBS_h)
      .def_readonly("vBS_W0", &Parameters::vBS_W0)
      .def_readonly("vBS_mf", &Parameters::vBS_mf)
      .def_readonly("vBS_b", &Parameters::vBS_b)
      .def_readonly("vBS_ef", &Parameters::vBS_ef)
      .def_readonly("mBS_l", &Parameters::mBS_l)
      .def_readonly("location", &Parameters::location)
      .def_readonly("start_date", &Parameters::start_date)
      .def_readonly("end_date", &Parameters::end_date)
      .def_readonly("vAlpha0", &Parameters::vAlpha0)
      .def_readonly("m", &Parameters::m)
      .def_readonly("n", &Parameters::n)
      .def_readonly("EGG", &Parameters::EGG)
      .def_readonly("LARVAE", &Parameters::LARVAE)
      .def_readonly("PUPAE", &Parameters::PUPAE)
      .def_readonly("ADULT1", &Parameters::ADULT1)
      .def_readonly("ADULT2", &Parameters::ADULT2)
      .def_readonly("WATER", &Parameters::WATER)
      .def_readonly("OVIPOSITION", &Parameters::OVIPOSITION)
      .def_readonly("weather", &Parameters::weather)
      .def_readonly("mf", &Parameters::mf);

    //https://pybind11.readthedocs.io/en/stable/classes.html
    pybind11::class_<Model>(m, "Model")
        .def(pybind11::init<const std::string>())
        .def("solveEquations", &Model::solveEquations)
        .def_readonly("start_date", &Model::start_date)
        .def_readonly("end_date", &Model::end_date)
        .def_readonly("time_range", &Model::time_range)
        .def_readonly("parameters", &Model::parameters)
        .def_readonly("Y", &Model::Y);


}
