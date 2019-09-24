# Specify the minimum version for CMake
cmake_minimum_required(VERSION 3.5)

file(WRITE ${CMAKE_BINARY_DIR}/ignore_me.cpp "
#include <foobar.h>
int ignorefoo(int a)
{
    return a * 42;
}
")

# Project's name
project(FASTCOV_TEST)

# Set the output folder where your program will be created
set(EXECUTABLE_OUTPUT_PATH ${CMAKE_BINARY_DIR}/bin)
set(LIBRARY_OUTPUT_PATH    ${CMAKE_BINARY_DIR}/lib)

set(CMAKE_CXX_STANDARD 14)

if (CMAKE_COMPILER_IS_GNUCXX)
    message("++ Enabling GCOV Code Coverage")
    set(CMAKE_C_FLAGS   "${CMAKE_C_FLAGS} -g -O0 -fprofile-arcs -ftest-coverage")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -g -O0 -fprofile-arcs -ftest-coverage")
endif (CMAKE_COMPILER_IS_GNUCXX)

enable_testing()

add_subdirectory(test)

add_library(untested SHARED
    untested.cpp
)