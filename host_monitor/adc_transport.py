"""ADS1263 command reads: status + four data bytes + checksum (INTERFACE=05).

Each poll completes a transaction. Never return an old sample or a bad checksum.
See TI ADS1263 sections 9.4.6 and 9.4.7.3.3.1.
"""
import time


def read_conversion(module, device, adc, timeout=.5):
    io = module.config
    command = module.ADS1263_CMD[f'CMD_RDATA{adc}']
    ready = 0x40 if adc == 1 else 0x80
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        io.digital_write(device.cs_pin, 0)
        try:
            io.spi_writebyte([command])
            packet = io.spi_readbytes(6)
        finally:
            io.digital_write(device.cs_pin, 1)
        if len(packet) != 6:
            raise IOError('Short ADC packet')
        if packet[0] & ready:
            data = packet[1:5] if adc == 1 else packet[1:4]
            if ((sum(data) + 0x9b) & 255) != packet[5]:
                raise IOError('ADC checksum mismatch')
            if adc == 1 and packet[0] & 0x1e:
                raise IOError('ADC1 reference/PGA alarm')
            if packet[0] & 1:
                raise IOError('ADC reset detected')
            if adc == 2 and packet[4] != 0:
                raise IOError('Invalid ADC2 padding')
            value = int.from_bytes(bytes(data), 'big', signed=True)
            if value in (-(1 << (8*len(data)-1)), (1 << (8*len(data)-1))-1):
                raise IOError('ADC saturated')
            return value
        time.sleep(.001)
    raise TimeoutError('ADC conversion timed out')
