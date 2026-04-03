# Test decode_four_byte with large values
params = ['255', '255', '255', '255']
result = 0
for i, part in enumerate(params[:4]):
    result += int(part) << (i * 8)
print(f'Result: {result}')
print(f'Result as 32-bit signed: {result if result < 2**31 else result - 2**32}')
print(f'Max uint32: {2**32 - 1}')
