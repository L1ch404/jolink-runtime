package fixture.child;

import lombok.extern.slf4j.Slf4j;

@Slf4j
public class ChildLogging {
    public void write() {
        childAudit.info("child-config");
    }
}
