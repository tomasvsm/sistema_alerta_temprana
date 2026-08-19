#ifndef TypesH
#define TypesH

#define EIGEN_INITIALIZE_MATRICES_BY_ZERO
#include <Eigen/Dense>

typedef double scalar;
typedef Eigen::Array<scalar,1,Eigen::Dynamic> tensor;//TODO:find a better name.This is a vector, with usual scalar-vector product and vector-vector sum, with addition to a vector-vector pointwise product.
typedef Eigen::Array<scalar,Eigen::Dynamic,Eigen::Dynamic> matrix;

#endif
