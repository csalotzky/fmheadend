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

# Add executable. Default name is the project name, version 0.1

add_executable(fmheadend_rp2354
        fmheadend_rp2354.c
        qn_bus.c
        qn_boot.c
        )

pico_set_program_name(fmheadend_rp2354 "fmheadend_rp2354")
pico_set_program_version(fmheadend_rp2354 "0.8.1")

# Modify the below lines to enable/disable output over UART/USB
# Explicit UART0 on PCB GPIO28/29; do not initialize default stdio pins.
pico_enable_stdio_uart(fmheadend_rp2354 0)
pico_enable_stdio_usb(fmheadend_rp2354 0)

# Add the standard library to the build
target_link_libraries(fmheadend_rp2354
        pico_stdlib
        hardware_uart)

# Add the standard include files to the build
target_include_directories(fmheadend_rp2354 PRIVATE
        ${CMAKE_CURRENT_LIST_DIR}
)

pico_add_extra_outputs(fmheadend_rp2354)

