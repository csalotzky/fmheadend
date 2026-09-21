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

add_executable(fmheadend_rp2354
        fmheadend_rp2354.c
        qn_bus.c
        qn_boot.c
        qn_control.c
        host_protocol.c
        host_i2c.c
        audio_samples.c
        audio_engine.c
        )

pico_set_program_name(fmheadend_rp2354 "fmheadend_rp2354")
pico_set_program_version(fmheadend_rp2354 "0.8.4")
pico_generate_pio_header(fmheadend_rp2354 ${CMAKE_CURRENT_LIST_DIR}/i2s_audio.pio)
target_compile_definitions(fmheadend_rp2354 PRIVATE
        FMHEADEND_HOST_I2C_ADDRESS=${HOST_ADDRESS_VALUE})

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
        pico_multicore)

# Add the standard include files to the build
target_include_directories(fmheadend_rp2354 PRIVATE
        ${CMAKE_CURRENT_LIST_DIR}
)

pico_add_extra_outputs(fmheadend_rp2354)

