package fixture;

import lombok.NonNull;
import lombok.RequiredArgsConstructor;

@RequiredArgsConstructor
public class RequiredFeature {
    @NonNull
    private final String value;
}
