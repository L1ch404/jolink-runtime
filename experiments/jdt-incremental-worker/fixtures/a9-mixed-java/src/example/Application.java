package example;

public class Application {
    private final Service service = new Service();

    public int answer() {
        return service.calculate(20);
    }
}
