package example;

final class BaseResponse<T> {
    private final T data;

    private BaseResponse(T data) {
        this.data = data;
    }

    static <T> BaseResponse<T> toSuccess(T data) {
        return new BaseResponse<T>(data);
    }

    T getData() {
        return data;
    }
}
