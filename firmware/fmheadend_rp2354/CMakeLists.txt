# Generated Cmake Pico project file

cmake_minimum_required(VERSION 3.18)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Initialise pico_sdk from installed location
# (note this can come from environment, CMake cache etc)

# == DO NOT EDIT THE FOLLOWING LINES for the Raspberry Pi Pico VS Code Extension to work ==
if(WIN32)
    set(USERHOME $ENV{USERPROFILE})
else()
    set(USERHOME $ENV{HOME})
endif()
set(sdkVersion 2.3.1)
set(toolchainVersion 15_2_Rel1)
set(picotoolVersion 2.3.1)
set(picoVscode ${USERHOME}/.pico-sdk/cmake/pico-vscode.cmake)
if (EXISTS ${picoVscode})
    include(${picoVscode})
endif()
# ====================================================================================
set(PICO_BOARD pico2 CACHE STRING "Board type")
# RP2354 board has 2 MiB, independent of the generic pico2 board default.
set(PICO_FLASH_SIZE_BYTES 2097152 CACHE STRING "Physical board flash bytes" FORCE)

# Pull in Raspberry Pi Pico SDK (must be before project)
include(pico_sdk_import.cmake)

project(fmheadend_rp2354 C CXX ASM)

# Initialise the Raspberry Pi Pico SDK
pico_sdk_init()

# Board deployment choice authorized for M8.2; protocol V1 has no universal address.
set(FMHEADEND_HOST_I2C_ADDRESS "0x62" CACHE STRING "RP host 7-bit I2C address (0x08..0x77)")
if(NOT FMHEADEND_HOST_I2C_ADDRESS MATCHES "^(0[xX][0-9a-fA-F]+|[0-9]+)$")
    message(FATAL_ERROR "FMHEADEND_HOST_I2C_ADDRESS must be a numeric 7-bit address")
endif()
math(EXPR HOST_ADDRESS_VALUE "${FMHEADEND_HOST_I2C_ADDRESS}")
if(HOST_ADDRESS_VALUE LESS 8 OR HOST_ADDRESS_VALUE GREATER 119)
    message(FATAL_ERROR "FMHEADEND_HOST_I2C_ADDRESS must be 0x08..0x77")
endif()

find_package(Python3 REQUIRED COMPONENTS Interpreter)
pico_init_picotool()

# Separate binary directories also isolate SDK-generated PIO/UF2 outputs.
set(firmware_dir "${CMAKE_CURRENT_SOURCE_DIR}")
set(FMHEADEND_IMAGE_MAJOR 1)
set(FMHEADEND_IMAGE_MINOR 0)
set(FMHEADEND_FACTORY_IMAGE ON)
set(target fmheadend_rp2354)
add_subdirectory(cmake/image factory)
set(FMHEADEND_IMAGE_MINOR 1)
set(FMHEADEND_FACTORY_IMAGE OFF)
set(target fmheadend_rp2354_ota)
add_subdirectory(cmake/image ota)

# Offline provisioning artifact; never contacts a device.
add_custom_command(OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/partitions.bin
    COMMAND $<TARGET_FILE:picotool> partition create
        ${CMAKE_CURRENT_LIST_DIR}/partitions.json ${CMAKE_CURRENT_BINARY_DIR}/partitions.bin
    DEPENDS ${CMAKE_CURRENT_LIST_DIR}/partitions.json
    VERBATIM)
add_custom_target(fmheadend_partitions ALL DEPENDS ${CMAKE_CURRENT_BINARY_DIR}/partitions.bin)
