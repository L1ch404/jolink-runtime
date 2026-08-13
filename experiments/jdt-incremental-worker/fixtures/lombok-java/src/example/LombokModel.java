package example;

import lombok.Builder;
import lombok.Data;
import lombok.NonNull;

@Data
@Builder
public class LombokModel {
    @NonNull
    private String name;
    private int count;

    public String normalize(@NonNull String value) {
        return value.trim();
    }
}
