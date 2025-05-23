cmake_minimum_required(VERSION 3.19)

# The exact name of this extension determines aspects of the installation and build paths, which need to be kept in sync
# with setup.py. Accordingly, take the name passed in by the caller, defaulting to "stack_v2" if needed.
set(EXTENSION_NAME
    "_stack_v2"
    CACHE STRING "Name of the extension")
project(${EXTENSION_NAME})
message(STATUS "Building extension: ${EXTENSION_NAME}")

# Custom cmake modules are in the parent directory
list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/../cmake")

# Includes
include(FetchContent)
include(ExternalProject)
include(AnalysisFunc)
include(FindCppcheck)

# dd_wrapper should be its own project at one point, if the current design is kept, but whether or not we keep that
# design is unknown.  Hack it for now.
add_subdirectory(../dd_wrapper ${CMAKE_CURRENT_BINARY_DIR}/../dd_wrapper_build)

find_package(Python3 COMPONENTS Interpreter Development)

# Make sure we have necessary Python variables
if(NOT Python3_INCLUDE_DIRS)
    message(FATAL_ERROR "Python3_INCLUDE_DIRS not found")
endif()

set(THREADS_PREFER_PTHREAD_FLAG ON)
find_package(Threads REQUIRED)

if(NOT Threads_FOUND OR NOT CMAKE_USE_PTHREADS_INIT)
    message(FATAL_ERROR "pthread compatible library not found")
endif()

# Add echion
set(ECHION_COMMIT
    "9d5bcc5867d7aefff73c837adcba4ef46eecebc6"
    CACHE STRING "Commit hash of echion to use")
FetchContent_Declare(
    echion
    GIT_REPOSITORY "https://github.com/taegyunkim/echion.git"
    GIT_TAG ${ECHION_COMMIT})
FetchContent_GetProperties(echion)

if(NOT echion_POPULATED)
    FetchContent_Populate(echion)
endif()

# Specify the target C-extension that we want to build
add_library(${EXTENSION_NAME} SHARED src/sampler.cpp src/stack_renderer.cpp src/stack_v2.cpp src/thread_span_links.cpp)

# Add common config
add_ddup_config(${EXTENSION_NAME})
add_cppcheck_target(
    ${EXTENSION_NAME}
    DIRECTORY
    ${CMAKE_CURRENT_SOURCE_DIR}
    INCLUDE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}/include/vendored
    ${CMAKE_CURRENT_SOURCE_DIR}/include/util
    ${CMAKE_CURRENT_SOURCE_DIR}/..
    ${echion_SOURCE_DIR}
    SRC
    ${CMAKE_CURRENT_SOURCE_DIR}/src)

# This project is build with C++17, even though the underlying repo adheres to the manylinux 2014 standard. This isn't
# currently a problem, but if it becomes one, we may have to structure the library differently.
target_compile_features(${EXTENSION_NAME} PUBLIC cxx_std_17)

# Never build with native unwinding, since this is not currently used
target_compile_definitions(${EXTENSION_NAME} PRIVATE UNWIND_NATIVE_DISABLE)

# Includes; echion and python are marked "system" to suppress warnings, but note in MSVC we'll have to #pragma
# warning(push, 0 then pop for the same effect.
target_include_directories(
    ${EXTENSION_NAME} PRIVATE .. # include dd_wrapper from the root in order to make its paths transparent in the code
    include)
target_include_directories(${EXTENSION_NAME} SYSTEM PRIVATE ${echion_SOURCE_DIR} ${Python3_INCLUDE_DIRS}
    include/vendored include/util)

# Echion sources need to be given the current platform
if(WIN32)
    target_compile_definitions(${EXTENSION_NAME} PRIVATE PL_WINDOWS)
elseif(APPLE)
    target_compile_definitions(${EXTENSION_NAME} PRIVATE PL_DARWIN)
elseif(UNIX)
    target_compile_definitions(${EXTENSION_NAME} PRIVATE PL_LINUX)
endif()

# cmake may mutate the name of the library (e.g., lib- and -.so for dynamic libraries). This suppresses that behavior,
# which is required to ensure all paths can be inferred correctly by setup.py.
set_target_properties(${EXTENSION_NAME} PROPERTIES PREFIX "")
set_target_properties(${EXTENSION_NAME} PROPERTIES SUFFIX "")

# RPATH is needed for sofile discovery at runtime, since Python packages are not installed in the system path. This is
# typical.
set_target_properties(${EXTENSION_NAME} PROPERTIES INSTALL_RPATH "$ORIGIN/..")

target_link_libraries(${EXTENSION_NAME} PRIVATE dd_wrapper Threads::Threads)

if(Python3_LIBRARIES)
    target_link_libraries(${EXTENSION_NAME} PRIVATE ${Python3_LIBRARIES})
endif()

# Extensions are built as dynamic libraries, so PIC is required.
set_target_properties(${EXTENSION_NAME} PROPERTIES POSITION_INDEPENDENT_CODE ON)

# Set the output directory for the built library
if(LIB_INSTALL_DIR)
    install(
        TARGETS ${EXTENSION_NAME}
        LIBRARY DESTINATION ${LIB_INSTALL_DIR}
        ARCHIVE DESTINATION ${LIB_INSTALL_DIR}
        RUNTIME DESTINATION ${LIB_INSTALL_DIR})
endif()
