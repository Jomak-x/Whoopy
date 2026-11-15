// Model-free diagnostic for pinned b10142 only, using its internal grammar API.
// Link with the pinned libllama and its runtime search path, then set
// GBNF_TEST_VALIDATOR to this executable when running test_llama_session_grammar.py.
#include <cstdint>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>
struct llama_grammar;
struct llama_vocab;
struct llama_grammar_element;
llama_grammar * llama_grammar_init_impl(const llama_vocab *, const char *, const char *, bool, const char **, size_t, const int32_t *, size_t);
void llama_grammar_free_impl(llama_grammar *);
void llama_grammar_accept_str(llama_grammar &, const std::string &);
std::vector<std::vector<const llama_grammar_element *>> & llama_grammar_get_stacks(llama_grammar *);
int main(int argc, char ** argv) {
    if (argc != 3) return 2;
    std::ifstream grammar_input(argv[1]);
    std::ifstream text_input(argv[2]);
    std::string grammar((std::istreambuf_iterator<char>(grammar_input)), {});
    std::string text((std::istreambuf_iterator<char>(text_input)), {});
    auto * state = llama_grammar_init_impl(nullptr, grammar.c_str(), "root", false, nullptr, 0, nullptr, 0);
    if (!state) return 3;
    bool complete = false;
    try {
        llama_grammar_accept_str(*state, text);
        for (const auto & stack : llama_grammar_get_stacks(state)) complete |= stack.empty();
    } catch (const std::exception &) {}
    llama_grammar_free_impl(state);
    std::cout << (complete ? "accepted" : "rejected") << "\n";
    return complete ? 0 : 1;
}
