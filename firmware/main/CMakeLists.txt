idf_component_register(
    SRCS
        "app_main.c"
        "wifi_app.c"
        "display.c"
        "content_client.c"
    INCLUDE_DIRS "."
    REQUIRES gdey_epd esp_http_client cjson
    PRIV_REQUIRES nvs_flash esp_wifi esp_event esp_netif esp_timer spiffs esp_app_format log
)
