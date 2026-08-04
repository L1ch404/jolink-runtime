package fixture;

import lombok.Builder;
import lombok.Data;
import lombok.NonNull;

@Data
@Builder
public class LombokFeatures {
    @NonNull
    private String name;
    private int count;

    public String requireValue(@NonNull String value) {
        return value;
    }
}
