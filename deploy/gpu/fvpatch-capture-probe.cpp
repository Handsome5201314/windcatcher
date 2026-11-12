#include <algorithm>
#include <execution>
#include <iostream>
#include <numeric>
#include <string>
#include <vector>

struct View {
    int* values;
    int& operator[](int index) const { return values[index]; }
};

int main(int argc, char** argv) {
    if (argc != 2) return 2;
    constexpr int count = 4096;
    std::vector<int> indices(count), input(count), output(count, 0);
    std::iota(indices.begin(), indices.end(), 0);
    std::iota(input.begin(), input.end(), 1);
    const View inputView{input.data()}, outputView{output.data()};
    const std::string mode = argv[1];
    if (mode == "reference") {
        std::for_each(std::execution::par_unseq, indices.begin(), indices.end(),
            [&](int index) { outputView[index] = inputView[index]; });
    } else if (mode == "view") {
        std::for_each(std::execution::par_unseq, indices.begin(), indices.end(),
            [inputView, outputView](int index) { outputView[index] = inputView[index]; });
    } else if (mode == "pointer") {
        const int* const source = input.data();
        int* const target = output.data();
        std::for_each(std::execution::par_unseq, indices.begin(), indices.end(),
            [source, target](int index) { target[index] = source[index]; });
    } else {
        return 2;
    }
    const long long total = std::accumulate(output.begin(), output.end(), 0LL);
    std::cout << mode << "_total=" << total << "\n";
    return total == (static_cast<long long>(count) * (count + 1)) / 2 ? 0 : 1;
}
