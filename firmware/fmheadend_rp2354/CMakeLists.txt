# Generated Cmake Pico project file

cmake_minimum_required(VERSION 3.13)

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

option(FMHEADEND_FACTORY_IMAGE "Non-TBYB initial provisioning image ONLY" OFF)
set(FMHEADEND_IMAGE_MAJOR 1 CACHE STRING "ROM image major (u16)")
set(FMHEADEND_IMAGE_MINOR 1 CACHE STRING "ROM image minor (u16); must increase for OTA")
foreach(v FMHEADEND_IMAGE_MAJOR FMHEADEND_IMAGE_MINOR)
    if(NOT "${${v}}" MATCHES "^[0-9]+$" OR ${${v}} GREATER 65535)
        message(FATAL_ERROR "${v} must be 0..65535")
    endif()
endforeach()
file(GLOB build_sources CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/*.c" "${CMAKE_CURRENT_SOURCE_DIR}/*.h" "${CMAKE_CURRENT_SOURCE_DIR}/*.pio")
list(APPEND build_sources "${CMAKE_CURRENT_SOURCE_DIR}/CMakeLists.txt")
set(build_fingerprint "${sdkVersion}/${toolchainVersion}/${FMHEADEND_IMAGE_MAJOR}.${FMHEADEND_IMAGE_MINOR}/${FMHEADEND_FACTORY_IMAGE}/${HOST_ADDRESS_VALUE}")
foreach(src IN LISTS build_sources)
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${src}")
    file(SHA256 "${src}" source_hash)
    string(APPEND build_fingerprint "${source_hash}")
endforeach()
string(SHA256 build_hash "${build_fingerprint}")
string(SUBSTRING "${build_hash}" 0 32 build_id)
string(REGEX REPLACE "([0-9a-f][0-9a-f])" "0x\\1," build_bytes "${build_id}")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/update_build.h" "#define FMHEADEND_BUILD_BYTES ${build_bytes}\n")
add_executable(fmheadend_rp2354
        fmheadend_rp2354.c
        qn_bus.c
        qn_boot.c
        qn_control.c
        flash_update.c
        flash_update_platform.c
        host_protocol.c
        rds_fifo.c
        rds_tx.c
        host_i2c.c
        audio_samples.c
        audio_engine.c
        spi_stream.c
        spi_audio.c
        )

pico_set_program_name(fmheadend_rp2354 "fmheadend_rp2354")
pico_set_program_version(fmheadend_rp2354 "0.8.6b")
pico_generate_pio_header(fmheadend_rp2354 ${CMAKE_CURRENT_LIST_DIR}/i2s_audio.pio)
pico_generate_pio_header(fmheadend_rp2354 ${CMAKE_CURRENT_LIST_DIR}/spi_audio.pio)
target_compile_definitions(fmheadend_rp2354 PRIVATE
        FMHEADEND_HOST_I2C_ADDRESS=${HOST_ADDRESS_VALUE}
        FMHEADEND_FLASH_UPDATE=1 PICO_FLASH_ASSERT_ON_UNSAFE=0 PICO_STACK_SIZE=0x1000 PICO_CORE1_STACK_SIZE=0x800
        PICO_CRT0_IMAGE_TYPE_TBYB=$<IF:$<BOOL:${FMHEADEND_FACTORY_IMAGE}>,0,1>
        PICO_CRT0_VERSION_MAJOR=${FMHEADEND_IMAGE_MAJOR}
        PICO_CRT0_VERSION_MINOR=${FMHEADEND_IMAGE_MINOR})

# Modify the below lines to enable/disable output over UART/USB
# Explicit UART0 on PCB GPIO28/29; do not initialize default stdio pins.
pico_enable_stdio_uart(fmheadend_rp2354 0)
pico_enable_stdio_usb(fmheadend_rp2354 0)

# Add the standard library to the build
target_link_libraries(fmheadend_rp2354
        pico_stdlib
        hardware_uart
        hardware_i2c
        hardware_pio
        hardware_dma
        pico_multicore
        pico_flash
        hardware_flash
        pico_sha256)

# Add the standard include files to the build
target_include_directories(fmheadend_rp2354 PRIVATE
        ${CMAKE_CURRENT_LIST_DIR}
        ${CMAKE_CURRENT_BINARY_DIR}
)

pico_add_extra_outputs(fmheadend_rp2354)


find_package(Python3 REQUIRED COMPONENTS Interpreter)
add_custom_command(TARGET fmheadend_rp2354 POST_BUILD
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_LIST_DIR}/../../tools/rp2354_artifact.py
        $<TARGET_FILE_DIR:fmheadend_rp2354>/fmheadend_rp2354.bin
        --build-id ${build_id} --major ${FMHEADEND_IMAGE_MAJOR} --minor ${FMHEADEND_IMAGE_MINOR}
        --factory $<IF:$<BOOL:${FMHEADEND_FACTORY_IMAGE}>,1,0>
    VERBATIM)

# Offline provisioning artifact; never contacts a device.
add_custom_command(OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/partitions.bin
    COMMAND $<TARGET_FILE:picotool> partition create
        ${CMAKE_CURRENT_LIST_DIR}/partitions.json ${CMAKE_CURRENT_BINARY_DIR}/partitions.bin
    DEPENDS ${CMAKE_CURRENT_LIST_DIR}/partitions.json
    VERBATIM)
add_custom_target(fmheadend_partitions ALL DEPENDS ${CMAKE_CURRENT_BINARY_DIR}/partitions.bin)
