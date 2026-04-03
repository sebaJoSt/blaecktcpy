import time

# Simulate the timer logic with interval_ms = 0
interval_ms = 0
base_ns = time.time_ns()
setpoint_ms = interval_ms  # 0

# Wait a tiny bit
time.sleep(0.001)

elapsed_ms = (time.time_ns() - base_ns) / 1_000_000
print(f'elapsed_ms: {elapsed_ms}')
print(f'setpoint_ms: {setpoint_ms}')
print(f'Condition: setpoint_ms <= elapsed_ms: {setpoint_ms <= elapsed_ms}')

# Simulate the while loop
iterations = 0
while setpoint_ms <= elapsed_ms and iterations < 10:
    setpoint_ms += interval_ms
    iterations += 1
    print(f'Iteration {iterations}: setpoint_ms = {setpoint_ms}')
    
print(f'Total iterations: {iterations}')
if iterations >= 10:
    print('WARNING: Loop would continue infinitely!')
