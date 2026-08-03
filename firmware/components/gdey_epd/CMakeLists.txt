idf_component_register(
    SRCS
        "gdey_epd.c"
        "gdeb0709e01.c"
    INCLUDE_DIRS "include"
    REQUIRES driver esp_driver_spi esp_driver_gpio
    PRIV_REQUIRES log
)
