idf_component_register(
    SRCS
        "app_main.c"
        "wifi_app.c"
        "wifi_config.c"
        "device_client.c"
        "display.c"
        "content_client.c"
        "ota_client.c"
        "http_util.c"
        "http_util_esp.c"
    INCLUDE_DIRS "."
    REQUIRES gdey_epd esp_http_client esp_http_server cjson app_update mbedtls
    PRIV_REQUIRES nvs_flash esp_wifi esp_event esp_netif esp_timer esp_app_format log esp_hw_support
)
