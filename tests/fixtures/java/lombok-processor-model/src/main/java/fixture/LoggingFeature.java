package fixture;

import lombok.extern.slf4j.Slf4j;

@Slf4j
public class LoggingFeature {
    public void write() {
        audit.info("root-config");
    }
}
