set(image_dir "${CMAKE_CURRENT_BINARY_DIR}")
file(MAKE_DIRECTORY "${image_dir}")
file(GLOB build_sources CONFIGURE_DEPENDS "${firmware_dir}/*.c" "${firmware_dir}/*.h" "${firmware_dir}/*.pio")
list(APPEND build_sources "${firmware_dir}/CMakeLists.txt" "${CMAKE_CURRENT_LIST_FILE}")
set(build_fingerprint "${sdkVersion}/${toolchainVersion}/${FMHEADEND_IMAGE_MAJOR}.${FMHEADEND_IMAGE_MINOR}/${FMHEADEND_FACTORY_IMAGE}/${HOST_ADDRESS_VALUE}/${TX_COUNT}/${FRAMES_PER_PACKET}/${DEBUG_UART_ENABLED}")
foreach(src IN LISTS build_sources)
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${src}")
    file(SHA256 "${src}" source_hash)
    string(APPEND build_fingerprint "${source_hash}")
endforeach()
string(SHA256 build_hash "${build_fingerprint}")
string(SUBSTRING "${build_hash}" 0 32 build_id)
string(REGEX REPLACE "([0-9a-f][0-9a-f])" "0x\\1," build_bytes "${build_id}")
file(CONFIGURE OUTPUT "${image_dir}/update_build.h" CONTENT "#define FMHEADEND_BUILD_BYTES ${build_bytes}\n")
add_executable(${target}
        ${firmware_dir}/fmheadend_rp2354.c
        ${firmware_dir}/qn_bus.c
        ${firmware_dir}/qn_boot.c
        ${firmware_dir}/qn_control.c
        ${firmware_dir}/flash_update.c
        ${firmware_dir}/flash_update_platform.c
        ${firmware_dir}/host_protocol.c
        ${firmware_dir}/rds_fifo.c
        ${firmware_dir}/rds_tx.c
        ${firmware_dir}/host_i2c.c
        ${firmware_dir}/audio_samples.c
        ${firmware_dir}/audio_engine.c
        ${firmware_dir}/spi_stream.c
        ${firmware_dir}/spi_audio.c
        )

pico_set_program_name(${target} "fmheadend_rp2354")
pico_set_program_version(${target} "${firmware_version}")
pico_generate_pio_header(${target} ${firmware_dir}/i2s_audio.pio)
pico_generate_pio_header(${target} ${firmware_dir}/spi_audio.pio)
target_compile_definitions(${target} PRIVATE
        TX_COUNT=${TX_COUNT} FRAMES_PER_PACKET=${FRAMES_PER_PACKET}
        DEBUG_UART_ENABLED=$<BOOL:${DEBUG_UART_ENABLED}>
        FMHEADEND_HOST_I2C_ADDRESS=${HOST_ADDRESS_VALUE}
        FMHEADEND_FLASH_UPDATE=1 PICO_FLASH_ASSERT_ON_UNSAFE=0 PICO_STACK_SIZE=0x1000 PICO_CORE1_STACK_SIZE=0x800
        PICO_CRT0_IMAGE_TYPE_TBYB=$<IF:$<BOOL:${FMHEADEND_FACTORY_IMAGE}>,0,1>
        PICO_CRT0_VERSION_MAJOR=${FMHEADEND_IMAGE_MAJOR}
        PICO_CRT0_VERSION_MINOR=${FMHEADEND_IMAGE_MINOR})

# Modify the below lines to enable/disable output over UART/USB
# Explicit UART0 on PCB GPIO28/29; do not initialize default stdio pins.
pico_enable_stdio_uart(${target} 0)
pico_enable_stdio_usb(${target} 0)

# Add the standard library to the build
target_link_libraries(${target}
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
target_include_directories(${target} PRIVATE
        ${firmware_dir}
        ${image_dir}
)

set_target_properties(${target} PROPERTIES OUTPUT_NAME fmheadend_rp2354_tx${TX_COUNT} RUNTIME_OUTPUT_DIRECTORY "${image_output_dir}")
pico_add_extra_outputs(${target})


find_package(Python3 REQUIRED COMPONENTS Interpreter)
add_custom_target(${target}_manifest ALL
    DEPENDS ${target}
    COMMAND ${Python3_EXECUTABLE} ${firmware_dir}/tools/rp2354_artifact.py
        ${image_output_dir}/fmheadend_rp2354_tx${TX_COUNT}.bin
        --build-id ${build_id} --major ${FMHEADEND_IMAGE_MAJOR} --minor ${FMHEADEND_IMAGE_MINOR}
        --factory $<IF:$<BOOL:${FMHEADEND_FACTORY_IMAGE}>,1,0>
    VERBATIM)

