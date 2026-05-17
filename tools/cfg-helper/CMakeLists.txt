cmake_minimum_required(VERSION 3.20)
project(cppgolf_cfg_helper LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

find_package(LLVM REQUIRED CONFIG)
find_package(Clang REQUIRED CONFIG)

add_executable(cppgolf-cfg-helper main.cpp)

target_include_directories(cppgolf-cfg-helper PRIVATE
  ${LLVM_INCLUDE_DIRS}
  ${CLANG_INCLUDE_DIRS}
)
target_compile_definitions(cppgolf-cfg-helper PRIVATE ${LLVM_DEFINITIONS})

target_link_libraries(cppgolf-cfg-helper PRIVATE
  clangTooling
  clangFrontend
  clangSerialization
  clangDriver
  clangParse
  clangSema
  clangAnalysis
  clangAST
  clangLex
  clangBasic
)
