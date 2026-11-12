#include <algorithm>
#include <execution>
#include <iostream>
#include <numeric>
#include <vector>

int main() {
    std::vector<int> values(4000000, 1);
    int increment = 1;
    std::for_each(std::execution::par_unseq, values.begin(), values.end(),
                  [&](int& value) { value += increment; });
    long long total = std::accumulate(values.begin(), values.end(), 0LL);
    std::cout << "stack_capture_total=" << total << '\n';
    return total == 8000000 ? 0 : 1;
}
