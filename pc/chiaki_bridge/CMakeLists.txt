# Dilmaç: chiaki-ng's library only (static) + bridge.c -> one DLL (chiaki_bridge.dll).
# Usage: build.sh (MSYS2 UCRT64). CHIAKI_SOURCE: the chiaki-ng source folder (with its submodules: nanopb,
# jerasure, gf-complete, curl).
cmake_minimum_required(VERSION 3.16)
project(dilmac_bridge C CXX)

set(CHIAKI_SOURCE "" CACHE PATH "chiaki-ng source folder")
if(NOT EXISTS "${CHIAKI_SOURCE}/lib/CMakeLists.txt")
	message(FATAL_ERROR "CHIAKI_SOURCE must point to the chiaki-ng source folder")
endif()

# No GUI, CLI, tests, video decoder, Steam Deck/Steam integration or internet (holepunch) connection needed.
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(CHIAKI_ENABLE_GUI OFF CACHE BOOL "" FORCE)
set(CHIAKI_ENABLE_CLI OFF CACHE BOOL "" FORCE)
set(CHIAKI_ENABLE_TESTS OFF CACHE BOOL "" FORCE)
set(CHIAKI_ENABLE_FFMPEG_DECODER OFF CACHE STRING "" FORCE)
set(CHIAKI_ENABLE_PI_DECODER OFF CACHE STRING "" FORCE)
set(CHIAKI_ENABLE_STEAMDECK_NATIVE OFF CACHE STRING "" FORCE)
set(CHIAKI_ENABLE_SETSU OFF CACHE STRING "" FORCE)
set(CHIAKI_ENABLE_SPEEX OFF CACHE STRING "" FORCE)
set(CHIAKI_ENABLE_STEAM_SHORTCUT OFF CACHE BOOL "" FORCE)
set(CHIAKI_LIB_ENABLE_OPUS ON CACHE BOOL "" FORCE)

add_subdirectory("${CHIAKI_SOURCE}" chiaki EXCLUDE_FROM_ALL)

add_library(bridge SHARED bridge.c)
target_link_libraries(bridge PRIVATE chiaki-lib)
set_target_properties(bridge PROPERTIES PREFIX "" OUTPUT_NAME "chiaki_bridge")
target_link_options(bridge PRIVATE -static-libgcc -static-libstdc++)
