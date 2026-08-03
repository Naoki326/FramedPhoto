idf_component_register(
    SRCS
        "app_main.c"
        "wifi_app.c"
        "display.c"
    INCLUDE_DIRS "."
    REQUIRES gdey_epd
    PRIV_REQUIRES nvs_flash esp_wifi esp_event esp_netif esp_timer esp_app_format log
)
