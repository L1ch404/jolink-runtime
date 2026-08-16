package example;

import java.util.ArrayList;
import java.util.List;

public final class RawAnonymousCollection {
    BaseResponse<List<String>> response(final String entity) {
        return BaseResponse.toSuccess(new ArrayList() {{
            add(entity);
        }});
    }
}
