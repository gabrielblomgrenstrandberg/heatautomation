# heatautomation
A script for selecting the best heat source based on spot price and temperature

## Test mode

Run the application without sending commands to KMP or Sensibo by setting
`HEATAUTOMATION_TEST_MODE=true` in the environment (or in the `.env` file) before
starting it. In this mode, the application logs the device actions it would
take and skips device connection checks. Set the variable to `false` or remove
it to enable normal operation.
