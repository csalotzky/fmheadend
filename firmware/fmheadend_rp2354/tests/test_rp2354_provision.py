import pytest
from rp2354_provision import FLASH_SIZE, FACTORY_OFFSET, verify_readback, tcl_path


def expected_flash():
    data=bytearray(b'\xff'*FLASH_SIZE)
    data[:3]=b'PT!'
    data[FACTORY_OFFSET:FACTORY_OFFSET+4]=b'APP!'
    return data


def test_full_flash_readback():
    verify_readback(expected_flash(),b'PT!',b'APP!')


@pytest.mark.parametrize('offset',[0,FACTORY_OFFSET,0xf4000,0x1fe000,FLASH_SIZE-1])
def test_readback_rejects_wrong_image_or_unerased_old_data(offset):
    data=expected_flash();data[offset]^=1
    with pytest.raises(ValueError,match='mismatch'):
        verify_readback(data,b'PT!',b'APP!')


def test_truncated_readback_is_not_success():
    with pytest.raises(ValueError):
        verify_readback(expected_flash()[:-1],b'PT!',b'APP!')


def test_tcl_paths_reject_command_delimiters(tmp_path):
    assert tcl_path(tmp_path/'with space.bin').endswith('with space.bin}')
    with pytest.raises(ValueError):tcl_path(tmp_path/'bad}name.bin')
