#include <algorithm>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <vector>

#include "clang/AST/ASTConsumer.h"
#include "clang/AST/Decl.h"
#include "clang/AST/DeclCXX.h"
#include "clang/AST/RecursiveASTVisitor.h"
#include "clang/Analysis/CFG.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendAction.h"
#include "clang/Lex/Lexer.h"
#include "clang/Tooling/CommonOptionsParser.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/ADT/StringSet.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/raw_ostream.h"

using namespace clang;
using namespace clang::tooling;

static llvm::cl::OptionCategory Category("cppgolf cfg helper");
static llvm::cl::list<std::string> FunctionNames(
    "function",
    llvm::cl::desc("Qualified or simple function name to dump. May be repeated."),
    llvm::cl::ZeroOrMore,
    llvm::cl::cat(Category));
static llvm::cl::opt<bool> FlowerPlan(
    "flower-plan",
    llvm::cl::desc("Emit safe insertion points for cppgolf flower obfuscation."),
    llvm::cl::init(false),
    llvm::cl::cat(Category));

struct StatementInfo {
  int Id = 0;
  const Stmt *Node = nullptr;
  SourceRange Range;
  std::string Kind;
  std::string Category;
};

struct LocalInfo {
  const ValueDecl *Decl = nullptr;
  int DeclStatementId = -1;
  int MustMergeUntilStatementId = -1;
  bool AddressTaken = false;
  bool CapturedByLambda = false;
  bool SafeHoist = false;
  bool SplitDeclInit = false;
  std::string HoistText;
  std::string ReplacementText;
  std::vector<SourceRange> Uses;
};

struct ControlFrame {
  int Id = -1;
  std::string Kind;
  bool IsLoop = false;
};

static llvm::json::Object blockPlanObject(ASTContext &Context, const Stmt *Body, int Depth,
                                          const std::vector<ControlFrame> &ControlStack,
                                          int ActiveLoopControlId, int &NextControlId);

static std::string qualifiedName(const FunctionDecl *Decl) {
  return Decl->getQualifiedNameAsString();
}

static bool matchesFunction(const FunctionDecl *Decl) {
  const std::string Qualified = qualifiedName(Decl);
  const std::string Simple = Decl->getNameAsString();
  if (FunctionNames.empty()) {
    return true;
  }
  for (const std::string &Name : FunctionNames) {
    if (Name == Qualified || Name == Simple) {
      return true;
    }
  }
  return false;
}

static std::string locationFile(const SourceManager &SM, SourceLocation Loc) {
  if (Loc.isInvalid()) {
    return "";
  }
  PresumedLoc Presumed = SM.getPresumedLoc(Loc);
  if (Presumed.isInvalid()) {
    return "";
  }
  return Presumed.getFilename();
}

static llvm::json::Object rangeObject(const ASTContext &Context, SourceLocation Begin,
                                      SourceLocation End) {
  const SourceManager &SM = Context.getSourceManager();
  llvm::json::Object Obj;
  if (Begin.isInvalid() || End.isInvalid()) {
    Obj["valid"] = false;
    return Obj;
  }
  CharSourceRange FileRange = Lexer::makeFileCharRange(
      CharSourceRange::getTokenRange(SourceRange(Begin, End)), SM, Context.getLangOpts());
  if (FileRange.isInvalid()) {
    Obj["valid"] = false;
    return Obj;
  }
  SourceLocation FileBegin = FileRange.getBegin();
  SourceLocation FileEnd = FileRange.getEnd();
  if (FileBegin.isInvalid() || FileEnd.isInvalid() || !FileBegin.isFileID() ||
      !FileEnd.isFileID() || SM.getFileID(FileBegin) != SM.getFileID(FileEnd)) {
    Obj["valid"] = false;
    return Obj;
  }
  Obj["valid"] = true;
  Obj["start"] = static_cast<int64_t>(SM.getFileOffset(FileBegin));
  Obj["end"] = static_cast<int64_t>(SM.getFileOffset(FileEnd));
  Obj["file"] = locationFile(SM, FileBegin);
  return Obj;
}

static llvm::json::Object rangeObject(const ASTContext &Context, SourceRange Range) {
  return rangeObject(Context, Range.getBegin(), Range.getEnd());
}

static std::string sourceText(const ASTContext &Context, SourceRange Range) {
  const SourceManager &SM = Context.getSourceManager();
  if (Range.getBegin().isInvalid() || Range.getEnd().isInvalid()) {
    return "";
  }
  CharSourceRange CharRange = CharSourceRange::getTokenRange(Range);
  bool Invalid = false;
  llvm::StringRef Text = Lexer::getSourceText(CharRange, SM, Context.getLangOpts(), &Invalid);
  if (Invalid) {
    return "";
  }
  return Text.str();
}

static bool isValidFileRange(const ASTContext &Context, SourceRange Range) {
  llvm::json::Object Obj = rangeObject(Context, Range);
  auto Valid = Obj.getBoolean("valid");
  return Valid.has_value() && *Valid;
}

static std::string macroKind(const ASTContext &Context, SourceRange Range) {
  const SourceManager &SM = Context.getSourceManager();
  SourceLocation Begin = Range.getBegin();
  SourceLocation End = Range.getEnd();
  if (Begin.isMacroID() || End.isMacroID()) {
    if (SM.isMacroArgExpansion(Begin) || SM.isMacroArgExpansion(End)) {
      return "arg_expansion";
    }
    return "expansion";
  }
  return "none";
}

static const char *elementKind(const CFGElement &Element) {
  switch (Element.getKind()) {
  case CFGElement::Statement:
    return "Statement";
  case CFGElement::Initializer:
    return "Initializer";
  case CFGElement::AutomaticObjectDtor:
    return "AutomaticObjectDtor";
  case CFGElement::DeleteDtor:
    return "DeleteDtor";
  case CFGElement::BaseDtor:
    return "BaseDtor";
  case CFGElement::MemberDtor:
    return "MemberDtor";
  case CFGElement::TemporaryDtor:
    return "TemporaryDtor";
  case CFGElement::Constructor:
    return "Constructor";
  case CFGElement::CXXRecordTypedCall:
    return "CXXRecordTypedCall";
  case CFGElement::ScopeBegin:
    return "ScopeBegin";
  case CFGElement::ScopeEnd:
    return "ScopeEnd";
  case CFGElement::LoopExit:
    return "LoopExit";
  case CFGElement::LifetimeEnds:
    return "LifetimeEnds";
  case CFGElement::NewAllocator:
    return "NewAllocator";
  case CFGElement::CleanupFunction:
    return "CleanupFunction";
  }
  return "Unknown";
}

static const Expr *conditionForTerminator(const Stmt *Terminator) {
  if (const auto *If = dyn_cast_or_null<IfStmt>(Terminator)) {
    return If->getCond();
  }
  if (const auto *While = dyn_cast_or_null<WhileStmt>(Terminator)) {
    return While->getCond();
  }
  if (const auto *Do = dyn_cast_or_null<DoStmt>(Terminator)) {
    return Do->getCond();
  }
  if (const auto *For = dyn_cast_or_null<ForStmt>(Terminator)) {
    return For->getCond();
  }
  if (const auto *Switch = dyn_cast_or_null<SwitchStmt>(Terminator)) {
    return Switch->getCond();
  }
  return nullptr;
}

static llvm::json::Object statementObject(ASTContext &Context, const Stmt *S) {
  llvm::json::Object Obj;
  Obj["kind"] = S->getStmtClassName();
  Obj["range"] = rangeObject(Context, S->getBeginLoc(), S->getEndLoc());
  return Obj;
}

static llvm::json::Object blockObject(ASTContext &Context, const CFG &Graph,
                                      const CFGBlock *Block) {
  llvm::json::Object Obj;
  Obj["id"] = static_cast<int64_t>(Block->getBlockID());
  Obj["entry"] = Block == &Graph.getEntry();
  Obj["exit"] = Block == &Graph.getExit();

  if (const Stmt *Terminator = Block->getTerminatorStmt()) {
    llvm::json::Object Term;
    Term["kind"] = Terminator->getStmtClassName();
    Term["range"] = rangeObject(Context, Terminator->getBeginLoc(), Terminator->getEndLoc());
    if (const Expr *Cond = conditionForTerminator(Terminator)) {
      Term["condition_range"] = rangeObject(Context, Cond->getBeginLoc(), Cond->getEndLoc());
    }
    Obj["terminator"] = std::move(Term);
  }

  llvm::json::Array Elements;
  llvm::json::Array Statements;
  for (const CFGElement &Element : *Block) {
    llvm::json::Object Elem;
    Elem["kind"] = elementKind(Element);
    if (std::optional<CFGStmt> Statement = Element.getAs<CFGStmt>()) {
      llvm::json::Object Stmt = statementObject(Context, Statement->getStmt());
      Statements.push_back(statementObject(Context, Statement->getStmt()));
      Elem["statement"] = std::move(Stmt);
    }
    Elements.push_back(std::move(Elem));
  }
  Obj["elements"] = std::move(Elements);
  Obj["statements"] = std::move(Statements);

  llvm::json::Array Succs;
  for (const CFGBlock::AdjacentBlock &Succ : Block->succs()) {
    if (const CFGBlock *SuccBlock = Succ.getReachableBlock()) {
      Succs.push_back(static_cast<int64_t>(SuccBlock->getBlockID()));
    } else {
      Succs.push_back(nullptr);
    }
  }
  Obj["successors"] = std::move(Succs);
  return Obj;
}

static bool containsStmtKind(const Stmt *S, bool (*Predicate)(const Stmt *)) {
  if (!S) {
    return false;
  }
  if (Predicate(S)) {
    return true;
  }
  for (const Stmt *Child : S->children()) {
    if (containsStmtKind(Child, Predicate)) {
      return true;
    }
  }
  return false;
}

static bool isGotoOrLabel(const Stmt *S) {
  return isa<GotoStmt>(S) || isa<IndirectGotoStmt>(S) || isa<LabelStmt>(S);
}

static bool isTryCatch(const Stmt *S) {
  return isa<CXXTryStmt>(S) || isa<CXXCatchStmt>(S) || isa<SEHTryStmt>(S) ||
         isa<SEHExceptStmt>(S) || isa<SEHFinallyStmt>(S);
}

static bool isLambda(const Stmt *S) { return isa<LambdaExpr>(S); }
static bool isSwitch(const Stmt *S) { return isa<SwitchStmt>(S); }
static bool isBreak(const Stmt *S) { return isa<BreakStmt>(S); }
static bool isContinue(const Stmt *S) { return isa<ContinueStmt>(S); }

static bool isLoopStmt(const Stmt *S) {
  return isa<ForStmt>(S) || isa<WhileStmt>(S) || isa<DoStmt>(S);
}

static std::string controlKind(const Stmt *S) {
  if (isa<ForStmt>(S)) {
    return "for";
  }
  if (isa<WhileStmt>(S)) {
    return "while";
  }
  if (isa<DoStmt>(S)) {
    return "do";
  }
  if (isa<SwitchStmt>(S)) {
    return "switch";
  }
  return "";
}

static std::optional<ControlFrame> breakTarget(const std::vector<ControlFrame> &Stack) {
  if (Stack.empty()) {
    return std::nullopt;
  }
  return Stack.back();
}

static std::optional<ControlFrame> continueTarget(const std::vector<ControlFrame> &Stack) {
  for (auto It = Stack.rbegin(); It != Stack.rend(); ++It) {
    if (It->IsLoop) {
      return *It;
    }
  }
  return std::nullopt;
}

static llvm::json::Object transferObject(ASTContext &Context, const Stmt *S,
                                         const std::vector<ControlFrame> &Stack) {
  llvm::json::Object Obj;
  const bool IsBreak = isa<BreakStmt>(S);
  Obj["kind"] = IsBreak ? "break" : "continue";
  Obj["range"] = rangeObject(Context, S->getSourceRange());
  const std::optional<ControlFrame> Target = IsBreak ? breakTarget(Stack) : continueTarget(Stack);
  if (Target.has_value()) {
    Obj["target_control_id"] = static_cast<int64_t>(Target->Id);
    Obj["target_kind"] = Target->Kind;
  } else {
    Obj["target_control_id"] = nullptr;
    Obj["target_kind"] = "";
  }
  const std::string Macro = macroKind(Context, S->getSourceRange());
  Obj["macro"] = Macro;
  Obj["safe"] = Macro == "none" && Target.has_value();
  return Obj;
}

static void collectTransfers(ASTContext &Context, const Stmt *S,
                             const std::vector<ControlFrame> &Stack,
                             llvm::json::Array &Output, bool IsRoot = false) {
  if (!S || isa<LambdaExpr>(S)) {
    return;
  }
  if (isa<BreakStmt>(S) || isa<ContinueStmt>(S)) {
    Output.push_back(transferObject(Context, S, Stack));
    return;
  }

  std::vector<ControlFrame> ChildStack = Stack;
  if (!IsRoot && (isLoopStmt(S) || isa<SwitchStmt>(S))) {
    // The root control statement's own frame is already included by
    // statementInfoObject(). Nested control statements get a synthetic frame
    // here only so transfers under them are not mistaken for the parent loop.
    if (ChildStack.empty() || ChildStack.back().Kind != controlKind(S)) {
      ChildStack.push_back(ControlFrame{-1, controlKind(S), isLoopStmt(S)});
    }
  }

  for (const Stmt *Child : S->children()) {
    collectTransfers(Context, Child, ChildStack, Output);
  }
}

static std::string categoryForStmt(const Stmt *S) {
  if (S->getBeginLoc().isMacroID() || S->getEndLoc().isMacroID()) {
    return "atomic";
  }
  if (isa<CompoundStmt>(S)) {
    return "block";
  }
  if (isa<DeclStmt>(S)) {
    return "decl";
  }
  if (isa<IfStmt>(S)) {
    return "if";
  }
  if (isa<ForStmt>(S) || isa<WhileStmt>(S) || isa<DoStmt>(S)) {
    return "loop";
  }
  if (isa<SwitchStmt>(S)) {
    return "switch";
  }
  if (isa<ReturnStmt>(S) || isa<CXXThrowExpr>(S) || isa<CoreturnStmt>(S)) {
    return "return";
  }
  if (containsStmtKind(S, isGotoOrLabel) || containsStmtKind(S, isTryCatch)) {
    return "atomic";
  }
  return "linear";
}

static llvm::json::Object containsObject(const Stmt *S) {
  llvm::json::Object Obj;
  Obj["lambda"] = containsStmtKind(S, isLambda);
  Obj["switch"] = containsStmtKind(S, isSwitch);
  Obj["goto"] = containsStmtKind(S, isGotoOrLabel);
  Obj["label"] = containsStmtKind(S, [](const Stmt *Node) { return isa<LabelStmt>(Node); });
  Obj["try"] = containsStmtKind(S, isTryCatch);
  Obj["break"] = containsStmtKind(S, isBreak);
  Obj["continue"] = containsStmtKind(S, isContinue);
  return Obj;
}

static llvm::json::Object controlObject(ASTContext &Context, const Stmt *S,
                                        const std::vector<ControlFrame> &ControlStack,
                                        int ControlId, int &NextControlId) {
  llvm::json::Object Obj;
  if (const auto *If = dyn_cast<IfStmt>(S)) {
    Obj["condition"] = rangeObject(Context, If->getCond()->getSourceRange());
    if (const Stmt *Then = If->getThen()) {
      Obj["then_body"] = rangeObject(Context, Then->getSourceRange());
      Obj["then_block"] = blockPlanObject(Context, Then, 1, ControlStack, -1, NextControlId);
    }
    if (const Stmt *Else = If->getElse()) {
      Obj["else_body"] = rangeObject(Context, Else->getSourceRange());
      Obj["else_block"] = blockPlanObject(Context, Else, 1, ControlStack, -1, NextControlId);
    }
    return Obj;
  }
  if (const auto *For = dyn_cast<ForStmt>(S)) {
    if (const Expr *Cond = For->getCond()) {
      Obj["condition"] = rangeObject(Context, Cond->getSourceRange());
    }
    if (const Stmt *Body = For->getBody()) {
      Obj["body"] = rangeObject(Context, Body->getSourceRange());
      Obj["body_block"] = blockPlanObject(Context, Body, 1, ControlStack, ControlId, NextControlId);
    }
    return Obj;
  }
  if (const auto *While = dyn_cast<WhileStmt>(S)) {
    Obj["condition"] = rangeObject(Context, While->getCond()->getSourceRange());
    if (const Stmt *Body = While->getBody()) {
      Obj["body"] = rangeObject(Context, Body->getSourceRange());
      Obj["body_block"] = blockPlanObject(Context, Body, 1, ControlStack, ControlId, NextControlId);
    }
    return Obj;
  }
  if (const auto *Do = dyn_cast<DoStmt>(S)) {
    Obj["condition"] = rangeObject(Context, Do->getCond()->getSourceRange());
    if (const Stmt *Body = Do->getBody()) {
      Obj["body"] = rangeObject(Context, Body->getSourceRange());
      Obj["body_block"] = blockPlanObject(Context, Body, 1, ControlStack, ControlId, NextControlId);
    }
    return Obj;
  }
  if (const auto *Switch = dyn_cast<SwitchStmt>(S)) {
    Obj["condition"] = rangeObject(Context, Switch->getCond()->getSourceRange());
    if (const Stmt *Body = Switch->getBody()) {
      Obj["body"] = rangeObject(Context, Body->getSourceRange());
    }
  }
  return Obj;
}

static llvm::json::Object statementInfoObject(ASTContext &Context, const StatementInfo &Info,
                                              const std::vector<ControlFrame> &ControlStack,
                                              int &NextControlId) {
  llvm::json::Object Obj;
  Obj["id"] = static_cast<int64_t>(Info.Id);
  Obj["kind"] = Info.Kind;
  Obj["category"] = Info.Category;
  Obj["range"] = rangeObject(Context, Info.Range);
  const std::string Macro = macroKind(Context, Info.Range);
  Obj["macro"] = Macro;
  int ControlId = -1;
  std::vector<ControlFrame> ChildStack = ControlStack;
  if (isLoopStmt(Info.Node) || isa<SwitchStmt>(Info.Node)) {
    ControlId = NextControlId++;
    const std::string Kind = controlKind(Info.Node);
    Obj["control_id"] = static_cast<int64_t>(ControlId);
    Obj["control_kind"] = Kind;
    ChildStack.push_back(ControlFrame{ControlId, Kind, isLoopStmt(Info.Node)});
  }
  Obj["control"] = controlObject(Context, Info.Node, ChildStack, ControlId, NextControlId);
  Obj["contains"] = containsObject(Info.Node);
  llvm::json::Array Transfers;
  collectTransfers(Context, Info.Node, ChildStack, Transfers, true);
  Obj["transfers"] = std::move(Transfers);
  if (isa<CompoundStmt>(Info.Node)) {
    Obj["block_plan"] = blockPlanObject(Context, Info.Node, 1, ChildStack, -1, NextControlId);
  }
  if (Macro != "none") {
    Obj["atomic_reason"] = "macro expansion";
  } else if (containsStmtKind(Info.Node, isLambda)) {
    Obj["atomic_reason"] = "lambda body is atomic";
  } else if (isa<SwitchStmt>(Info.Node) || containsStmtKind(Info.Node, isSwitch)) {
    Obj["atomic_reason"] = "switch body is atomic";
  }
  return Obj;
}

static bool isTrivialType(QualType Type) {
  Type = Type.getCanonicalType();
  if (Type->isBuiltinType() || Type->isPointerType() || Type->isReferenceType() ||
      Type->isEnumeralType()) {
    return true;
  }
  if (const auto *Record = Type->getAsCXXRecordDecl()) {
    return Record->hasTrivialDestructor() && Record->hasTrivialDefaultConstructor();
  }
  return false;
}

static bool isDefaultConstructible(QualType Type) {
  Type = Type.getCanonicalType();
  while (const auto *Array = dyn_cast<ArrayType>(Type.getTypePtr())) {
    Type = Array->getElementType().getCanonicalType();
  }
  if (Type->isReferenceType() || Type.isConstQualified()) {
    return false;
  }
  if (Type->isBuiltinType() || Type->isPointerType() || Type->isEnumeralType()) {
    return true;
  }
  if (const auto *Record = Type->getAsCXXRecordDecl()) {
    return Record->hasDefaultConstructor();
  }
  return false;
}

static bool isSafeSplitInitType(QualType Type) {
  Type = Type.getCanonicalType();
  if (Type->isReferenceType() || Type.isConstQualified() || Type->isArrayType()) {
    return false;
  }
  return Type->isBuiltinType() || Type->isPointerType() || Type->isEnumeralType();
}

static bool canHoistDecl(const VarDecl *Decl, bool EscapesOrCrossesCase) {
  if (!EscapesOrCrossesCase) {
    return false;
  }
  if (!Decl->hasLocalStorage() || Decl->isStaticLocal()) {
    return false;
  }
  if (Decl->getType()->isArrayType()) {
    return false;
  }
  if (Decl->hasInit() && Decl->getInitStyle() != VarDecl::CallInit) {
    return false;
  }
  return isDefaultConstructible(Decl->getType());
}

static bool canSplitDeclInit(const VarDecl *Decl, bool EscapesOrCrossesCase) {
  if (!EscapesOrCrossesCase || !Decl->hasLocalStorage() || Decl->isStaticLocal()) {
    return false;
  }
  if (!Decl->hasInit() || Decl->getName().empty()) {
    return false;
  }
  return isSafeSplitInitType(Decl->getType());
}

static std::string hoistedDeclText(ASTContext &Context, const VarDecl *Decl) {
  if (!Decl || !Decl->getTypeSourceInfo()) {
    return "";
  }
  const std::string SourceTypeText =
      sourceText(Context, Decl->getTypeSourceInfo()->getTypeLoc().getSourceRange());
  if (SourceTypeText.empty() || SourceTypeText.find("auto") != std::string::npos) {
    return "";
  }
  PrintingPolicy Policy(Context.getLangOpts());
  const std::string TypeText = Decl->getType().getAsString(Policy);
  if (TypeText.empty()) {
    return "";
  }
  return TypeText + " " + Decl->getNameAsString() + ";";
}

static const ValueDecl *canonicalLocalDecl(const ValueDecl *Decl) {
  if (!Decl) {
    return nullptr;
  }
  if (const auto *Var = dyn_cast<VarDecl>(Decl)) {
    return Var->getCanonicalDecl();
  }
  if (isa<BindingDecl>(Decl)) {
    return Decl;
  }
  return nullptr;
}

class LocalUseVisitor : public RecursiveASTVisitor<LocalUseVisitor> {
public:
  explicit LocalUseVisitor(std::map<const ValueDecl *, LocalInfo> &Locals)
      : Locals(Locals) {}

  bool VisitDeclRefExpr(DeclRefExpr *Expr) {
    const auto *Decl = dyn_cast_or_null<ValueDecl>(Expr->getDecl());
    if (!Decl) {
      return true;
    }
    auto It = Locals.find(canonicalLocalDecl(Decl));
    if (It != Locals.end()) {
      It->second.Uses.push_back(Expr->getSourceRange());
    }
    return true;
  }

  bool TraverseUnaryOperator(UnaryOperator *Op) {
    if (Op && Op->getOpcode() == UO_AddrOf) {
      if (const auto *Ref = dyn_cast<DeclRefExpr>(Op->getSubExpr()->IgnoreParenImpCasts())) {
        const auto *Decl = dyn_cast_or_null<ValueDecl>(Ref->getDecl());
        if (Decl) {
          auto It = Locals.find(canonicalLocalDecl(Decl));
          if (It != Locals.end()) {
            It->second.AddressTaken = true;
          }
        }
      }
    }
    return RecursiveASTVisitor<LocalUseVisitor>::TraverseUnaryOperator(Op);
  }

  bool TraverseImplicitCastExpr(ImplicitCastExpr *Expr) {
    if (Expr && Expr->getCastKind() == CK_ArrayToPointerDecay) {
      if (const auto *Ref = dyn_cast<DeclRefExpr>(Expr->getSubExpr()->IgnoreParenImpCasts())) {
        const auto *Decl = dyn_cast_or_null<ValueDecl>(Ref->getDecl());
        if (Decl) {
          auto It = Locals.find(canonicalLocalDecl(Decl));
          if (It != Locals.end()) {
            It->second.AddressTaken = true;
          }
        }
      }
    }
    return RecursiveASTVisitor<LocalUseVisitor>::TraverseImplicitCastExpr(Expr);
  }

  bool TraverseLambdaExpr(LambdaExpr *Expr) {
    if (!Expr) {
      return true;
    }
    for (const LambdaCapture &Capture : Expr->captures()) {
      if (const auto *Decl = dyn_cast_or_null<ValueDecl>(Capture.getCapturedVar())) {
        auto It = Locals.find(canonicalLocalDecl(Decl));
        if (It != Locals.end()) {
          It->second.CapturedByLambda = true;
        }
      }
    }
    return RecursiveASTVisitor<LocalUseVisitor>::TraverseLambdaExpr(Expr);
  }

private:
  std::map<const ValueDecl *, LocalInfo> &Locals;
};

static int statementIndexForOffset(const ASTContext &Context,
                                   const std::vector<StatementInfo> &Statements,
                                   SourceLocation Loc) {
  const SourceManager &SM = Context.getSourceManager();
  if (Loc.isInvalid() || !Loc.isFileID()) {
    return -1;
  }
  unsigned Offset = SM.getFileOffset(Loc);
  for (size_t Index = 0; Index < Statements.size(); ++Index) {
    llvm::json::Object Range = rangeObject(Context, Statements[Index].Range);
    auto Start = Range.getInteger("start");
    auto End = Range.getInteger("end");
    if (Start && End && static_cast<int64_t>(Offset) >= *Start &&
        static_cast<int64_t>(Offset) <= *End) {
      return static_cast<int>(Index);
    }
  }
  return -1;
}

static int statementIdForIndex(const std::vector<StatementInfo> &Statements, int Index) {
  if (Index < 0 || static_cast<size_t>(Index) >= Statements.size()) {
    return -1;
  }
  return Statements[static_cast<size_t>(Index)].Id;
}

static llvm::json::Object localObject(ASTContext &Context, const LocalInfo &Info) {
  llvm::json::Object Obj;
  Obj["name"] = Info.Decl->getNameAsString();
  Obj["decl_statement_id"] = static_cast<int64_t>(Info.DeclStatementId);
  Obj["decl_range"] = rangeObject(Context, Info.Decl->getSourceRange());
  if (const auto *Var = dyn_cast<VarDecl>(Info.Decl);
      Var && Var->getInit()) {
    const Expr *Init = Var->getInit();
    Obj["init_range"] = rangeObject(Context, Init->getSourceRange());
  } else {
    Obj["init_range"] = llvm::json::Object{{"valid", false}};
  }
  Obj["type"] = Info.Decl->getType().getAsString();
  Obj["is_trivial"] = isTrivialType(Info.Decl->getType());
  Obj["is_default_constructible"] = isDefaultConstructible(Info.Decl->getType());
  Obj["address_taken"] = Info.AddressTaken;
  Obj["captured_by_lambda"] = Info.CapturedByLambda;
  Obj["safe_hoist"] = Info.SafeHoist;
  Obj["split_decl_init"] = Info.SplitDeclInit;
  if (Info.SplitDeclInit) {
    Obj["hoist_text"] = Info.HoistText;
    Obj["replacement_text"] = Info.ReplacementText;
  }
  if (Info.MustMergeUntilStatementId >= 0) {
    Obj["must_merge_until_statement_id"] = static_cast<int64_t>(Info.MustMergeUntilStatementId);
  } else {
    Obj["must_merge_until_statement_id"] = nullptr;
  }
  llvm::json::Array Uses;
  for (SourceRange Use : Info.Uses) {
    Uses.push_back(rangeObject(Context, Use));
  }
  Obj["uses"] = std::move(Uses);
  return Obj;
}

static std::vector<StatementInfo> collectTopLevelStatements(ASTContext &Context,
                                                            const Stmt *Body) {
  std::vector<StatementInfo> Result;
  const auto *Compound = dyn_cast_or_null<CompoundStmt>(Body);
  if (!Compound) {
    return Result;
  }
  int Id = 1;
  for (const Stmt *Child : Compound->body()) {
    if (!Child || !isValidFileRange(Context, Child->getSourceRange())) {
      continue;
    }
    StatementInfo Info;
    Info.Id = Id++;
    Info.Node = Child;
    Info.Range = Child->getSourceRange();
    Info.Kind = Child->getStmtClassName();
    Info.Category = categoryForStmt(Child);
    Result.push_back(Info);
  }
  return Result;
}

static bool hasInvalidDirectChildRange(ASTContext &Context, const Stmt *Body) {
  const auto *Compound = dyn_cast_or_null<CompoundStmt>(Body);
  if (!Compound) {
    return false;
  }
  for (const Stmt *Child : Compound->body()) {
    if (!Child || !isValidFileRange(Context, Child->getSourceRange())) {
      return true;
    }
  }
  return false;
}

static llvm::json::Array invalidDirectChildRangeDiagnostics(ASTContext &Context,
                                                            const Stmt *Body) {
  llvm::json::Array Diagnostics;
  const auto *Compound = dyn_cast_or_null<CompoundStmt>(Body);
  if (!Compound) {
    return Diagnostics;
  }
  int Index = 0;
  for (const Stmt *Child : Compound->body()) {
    ++Index;
    if (Child && isValidFileRange(Context, Child->getSourceRange())) {
      continue;
    }
    std::string Message = "invalid direct statement range";
    Message += " at child ";
    Message += std::to_string(Index);
    Message += " (";
    Message += Child ? Child->getStmtClassName() : "null";
    Message += ")";
    Diagnostics.push_back(Message);
  }
  return Diagnostics;
}

static void addLocalInfo(std::map<const ValueDecl *, LocalInfo> &Locals,
                         const ValueDecl *Decl, int StatementId) {
  const ValueDecl *Canonical = canonicalLocalDecl(Decl);
  if (!Canonical) {
    return;
  }
  LocalInfo Info;
  Info.Decl = Canonical;
  Info.DeclStatementId = StatementId;
  Locals[Canonical] = Info;
}

static const VarDecl *singleLocalVarDecl(const Stmt *Statement) {
  const auto *DeclStatement = dyn_cast_or_null<DeclStmt>(Statement);
  if (!DeclStatement || !DeclStatement->isSingleDecl()) {
    return nullptr;
  }
  const auto *Var = dyn_cast_or_null<VarDecl>(DeclStatement->getSingleDecl());
  if (!Var || !Var->hasLocalStorage() || isa<ParmVarDecl>(Var)) {
    return nullptr;
  }
  return Var;
}

static llvm::json::Array collectLocals(ASTContext &Context,
                                       const std::vector<StatementInfo> &Statements) {
  std::map<const ValueDecl *, LocalInfo> Locals;
  for (const StatementInfo &Statement : Statements) {
    const auto *DeclStatement = dyn_cast_or_null<DeclStmt>(Statement.Node);
    if (!DeclStatement) {
      continue;
    }
    for (const Decl *D : DeclStatement->decls()) {
      if (const auto *Var = dyn_cast<VarDecl>(D)) {
        if (Var->hasLocalStorage() && !isa<ParmVarDecl>(Var)) {
          addLocalInfo(Locals, Var, Statement.Id);
          if (const auto *Decomp = dyn_cast<DecompositionDecl>(Var)) {
            for (const BindingDecl *Binding : Decomp->bindings()) {
              addLocalInfo(Locals, Binding, Statement.Id);
            }
          }
        }
      }
    }
  }
  LocalUseVisitor Visitor(Locals);
  for (const StatementInfo &Statement : Statements) {
    Visitor.TraverseStmt(const_cast<Stmt *>(Statement.Node));
  }

  for (auto &Entry : Locals) {
    LocalInfo &Info = Entry.second;
    int DeclIndex = -1;
    for (size_t Index = 0; Index < Statements.size(); ++Index) {
      if (Statements[Index].Id == Info.DeclStatementId) {
        DeclIndex = static_cast<int>(Index);
        break;
      }
    }
    int LastUseIndex = DeclIndex;
    for (SourceRange Use : Info.Uses) {
      int UseIndex = statementIndexForOffset(Context, Statements, Use.getBegin());
      if (UseIndex > LastUseIndex) {
        LastUseIndex = UseIndex;
      }
    }
    const bool CrossesCase = LastUseIndex > DeclIndex;
    const bool Escapes = Info.AddressTaken || Info.CapturedByLambda;
    if (const auto *Var = dyn_cast<VarDecl>(Info.Decl)) {
      Info.SafeHoist = canHoistDecl(Var, CrossesCase || Escapes);
      if (!Info.SafeHoist && canSplitDeclInit(Var, CrossesCase || Escapes)) {
        const VarDecl *Single = singleLocalVarDecl(Statements[static_cast<size_t>(DeclIndex)].Node);
        if (Single && Single->getCanonicalDecl() == Var->getCanonicalDecl()) {
          const std::string InitText = sourceText(Context, Var->getInit()->getSourceRange());
          const std::string HoistText = hoistedDeclText(Context, Var);
          if (!InitText.empty() && !HoistText.empty()) {
            Info.SplitDeclInit = true;
            Info.HoistText = HoistText;
            Info.ReplacementText = Var->getNameAsString() + " = " + InitText + ";";
          }
        }
      }
    } else {
      Info.SafeHoist = false;
    }
    if (!Info.SafeHoist && !Info.SplitDeclInit &&
        (CrossesCase || Escapes || !isTrivialType(Info.Decl->getType()))) {
      Info.MustMergeUntilStatementId = statementIdForIndex(Statements, LastUseIndex);
    }
  }

  llvm::json::Array Output;
  for (const auto &Entry : Locals) {
    Output.push_back(localObject(Context, Entry.second));
  }
  return Output;
}

static llvm::json::Object blockPlanObject(ASTContext &Context, const Stmt *Body, int Depth,
                                          const std::vector<ControlFrame> &ControlStack,
                                          int ActiveLoopControlId, int &NextControlId) {
  llvm::json::Object Block;
  if (!Body || !isValidFileRange(Context, Body->getSourceRange())) {
    Block["range"] = llvm::json::Object{{"valid", false}};
    Block["statements"] = llvm::json::Array();
    Block["locals"] = llvm::json::Array();
    Block["diagnostics"] = llvm::json::Array{"invalid block range"};
    return Block;
  }

  Block["range"] = rangeObject(Context, Body->getSourceRange());
  if (ActiveLoopControlId >= 0) {
    Block["active_loop_control_id"] = static_cast<int64_t>(ActiveLoopControlId);
  } else {
    Block["active_loop_control_id"] = nullptr;
  }
  llvm::json::Array Diagnostics;
  if (containsStmtKind(Body, isGotoOrLabel) || containsStmtKind(Body, isTryCatch)) {
    Diagnostics.push_back("unsupported goto/label/try structure");
  }
  for (auto Diagnostic : invalidDirectChildRangeDiagnostics(Context, Body)) {
    Diagnostics.push_back(std::move(Diagnostic));
  }
  if (Depth > 8) {
    Diagnostics.push_back("maximum helper recursion depth reached");
  }

  std::vector<StatementInfo> Statements;
  if (Depth <= 8) {
    Statements = collectTopLevelStatements(Context, Body);
    if (Statements.empty() && !isa<CompoundStmt>(Body)) {
      StatementInfo Info;
      Info.Id = 1;
      Info.Node = Body;
      Info.Range = Body->getSourceRange();
      Info.Kind = Body->getStmtClassName();
      Info.Category = categoryForStmt(Body);
      Statements.push_back(Info);
    }
  }

  llvm::json::Array StatementJson;
  for (const StatementInfo &Statement : Statements) {
    StatementJson.push_back(statementInfoObject(Context, Statement, ControlStack, NextControlId));
  }
  Block["statements"] = std::move(StatementJson);
  Block["locals"] = collectLocals(Context, Statements);
  Block["diagnostics"] = std::move(Diagnostics);
  return Block;
}

static bool hasUnsupportedTopLevelStructure(const std::vector<StatementInfo> &Statements) {
  for (const StatementInfo &Statement : Statements) {
    if (containsStmtKind(Statement.Node, isGotoOrLabel) ||
        containsStmtKind(Statement.Node, isTryCatch)) {
      return true;
    }
  }
  return false;
}

static bool rangeIsWrittenInMainFile(const ASTContext &Context, SourceRange Range) {
  const SourceManager &SM = Context.getSourceManager();
  llvm::json::Object RangeJson = rangeObject(Context, Range);
  auto Valid = RangeJson.getBoolean("valid");
  if (!Valid || !*Valid) {
    return false;
  }
  SourceLocation Begin = Lexer::makeFileCharRange(
                             CharSourceRange::getTokenRange(Range), SM, Context.getLangOpts())
                             .getBegin();
  return Begin.isValid() && SM.isWrittenInMainFile(Begin);
}

static std::optional<int64_t> rangeStartOffset(ASTContext &Context, SourceRange Range) {
  llvm::json::Object RangeJson = rangeObject(Context, Range);
  auto Valid = RangeJson.getBoolean("valid");
  auto Start = RangeJson.getInteger("start");
  if (!Valid || !*Valid || !Start) {
    return std::nullopt;
  }
  return *Start;
}

static std::optional<int64_t> rangeEndOffset(ASTContext &Context, SourceRange Range) {
  llvm::json::Object RangeJson = rangeObject(Context, Range);
  auto Valid = RangeJson.getBoolean("valid");
  auto End = RangeJson.getInteger("end");
  if (!Valid || !*Valid || !End) {
    return std::nullopt;
  }
  return *End;
}

static bool isExternCContext(const Decl *Node) {
  for (const DeclContext *Context = Node ? Node->getDeclContext() : nullptr; Context;
       Context = Context->getParent()) {
    if (const auto *Linkage = dyn_cast<LinkageSpecDecl>(Context)) {
      if (Linkage->getLanguage() == LinkageSpecLanguageIDs::C) {
        return true;
      }
    }
  }
  return false;
}

static void collectCompoundInsertOffsets(ASTContext &Context, const Stmt *Node,
                                         std::set<int64_t> &Offsets) {
  if (!Node || isa<LambdaExpr>(Node) || isa<SwitchStmt>(Node)) {
    return;
  }
  if (const auto *Compound = dyn_cast<CompoundStmt>(Node)) {
    if (rangeIsWrittenInMainFile(Context, Compound->getSourceRange())) {
      std::optional<int64_t> Start = rangeStartOffset(Context, Compound->getSourceRange());
      std::optional<int64_t> End = rangeEndOffset(Context, Compound->getSourceRange());
      if (Start && End && *End > *Start + 1) {
        Offsets.insert(*Start + 1);
        bool HasDirectCaseLabel = false;
        for (const Stmt *Child : Compound->body()) {
          if (Child && (isa<CaseStmt>(Child) || isa<DefaultStmt>(Child))) {
            HasDirectCaseLabel = true;
            break;
          }
        }
        if (!HasDirectCaseLabel) {
          for (const Stmt *Child : Compound->body()) {
            if (!Child || !rangeIsWrittenInMainFile(Context, Child->getSourceRange())) {
              continue;
            }
            std::optional<int64_t> ChildEnd = rangeEndOffset(Context, Child->getSourceRange());
            if (ChildEnd && *ChildEnd > *Start && *ChildEnd < *End) {
              Offsets.insert(*ChildEnd);
            }
          }
        }
      }
    }
  }
  for (const Stmt *Child : Node->children()) {
    collectCompoundInsertOffsets(Context, Child, Offsets);
  }
}

static llvm::json::Object flowerFunctionObject(ASTContext &Context, FunctionDecl *Decl) {
  Stmt *Body = Decl->getBody();
  llvm::json::Object Func;
  Func["qualified_name"] = qualifiedName(Decl);
  Func["simple_name"] = Decl->getNameAsString();
  Func["body"] = rangeObject(Context, Body->getBeginLoc(), Body->getEndLoc());
  Func["is_constexpr"] = Decl->isConstexpr();
  Func["is_consteval"] = Decl->isConsteval();
  Func["is_extern_c"] = Decl->isExternC() || isExternCContext(Decl);

  llvm::json::Array Diagnostics;
  if (!rangeIsWrittenInMainFile(Context, Body->getSourceRange())) {
    Diagnostics.push_back("body is not in main file");
  }
  if (Decl->isConstexpr() || Decl->isConsteval()) {
    Diagnostics.push_back("constexpr/consteval function");
  }
  if (Decl->isExternC() || isExternCContext(Decl)) {
    Diagnostics.push_back("extern C function");
  }
  if (containsStmtKind(Body, isGotoOrLabel) || containsStmtKind(Body, isTryCatch)) {
    Diagnostics.push_back("unsupported goto/label/try structure");
  }
  if (Body->getBeginLoc().isMacroID() || Body->getEndLoc().isMacroID()) {
    Diagnostics.push_back("macro body range");
  }

  std::set<int64_t> Offsets;
  if (Diagnostics.empty()) {
    collectCompoundInsertOffsets(Context, Body, Offsets);
  }
  llvm::json::Array OffsetJson;
  for (int64_t Offset : Offsets) {
    OffsetJson.push_back(Offset);
  }
  Func["insert_offsets"] = std::move(OffsetJson);
  Func["diagnostics"] = std::move(Diagnostics);
  return Func;
}

static bool isSafeScopeChildDecl(ASTContext &Context, const Decl *Node) {
  if (!Node || Node->isImplicit() || isa<AccessSpecDecl>(Node) ||
      isa<ClassTemplateSpecializationDecl>(Node)) {
    return false;
  }
  SourceRange Range = Node->getSourceRange();
  if (Range.getBegin().isInvalid() || Range.getEnd().isInvalid() ||
      Range.getBegin().isMacroID() || Range.getEnd().isMacroID()) {
    return false;
  }
  return rangeIsWrittenInMainFile(Context, Range);
}

static std::set<int64_t> scopeInsertOffsets(ASTContext &Context, const DeclContext *Scope,
                                            int64_t Start, int64_t End, int64_t Fallback) {
  std::set<int64_t> Offsets;
  if (!Scope || End <= Start) {
    return Offsets;
  }
  for (const Decl *Child : Scope->decls()) {
    if (!isSafeScopeChildDecl(Context, Child)) {
      continue;
    }
    std::optional<int64_t> ChildEnd = rangeEndOffset(Context, Child->getSourceRange());
    if (ChildEnd && *ChildEnd > Start && *ChildEnd < End) {
      Offsets.insert(*ChildEnd);
    }
  }
  if (Fallback >= Start && Fallback <= End) {
    Offsets.insert(Fallback);
  }
  return Offsets;
}

static llvm::json::Object flowerScopeObject(ASTContext &Context, llvm::StringRef Kind,
                                            llvm::StringRef Name, const DeclContext *DeclCtx,
                                            SourceRange Range) {
  llvm::json::Object Scope;
  Scope["kind"] = Kind.str();
  Scope["name"] = Name.str();
  Scope["range"] = rangeObject(Context, Range);
  std::optional<int64_t> Start = rangeStartOffset(Context, Range);
  std::optional<int64_t> End = rangeEndOffset(Context, Range);
  llvm::json::Array Offsets;
  if (Start && End && *End > *Start) {
    int64_t Fallback = *End - 1;
    for (int64_t Offset : scopeInsertOffsets(Context, DeclCtx, *Start, *End, Fallback)) {
      Offsets.push_back(Offset);
    }
    Scope["insert_offset"] = Fallback;
  } else {
    Scope["insert_offset"] = nullptr;
  }
  Scope["insert_offsets"] = std::move(Offsets);
  return Scope;
}

class FlowerVisitor : public RecursiveASTVisitor<FlowerVisitor> {
public:
  FlowerVisitor(ASTContext &Context, llvm::json::Array &Functions, llvm::json::Array &Scopes)
      : Context(Context), Functions(Functions), Scopes(Scopes) {}

  bool TraverseDecl(Decl *Node) {
    if (!Node) {
      return true;
    }
    if (isa<LinkageSpecDecl>(Node)) {
      return true;
    }
    return RecursiveASTVisitor<FlowerVisitor>::TraverseDecl(Node);
  }

  bool VisitFunctionDecl(FunctionDecl *Decl) {
    if (!Decl || !Decl->hasBody() || !matchesFunction(Decl) || Decl->isImplicit()) {
      return true;
    }
    if (!rangeIsWrittenInMainFile(Context, Decl->getBody()->getSourceRange())) {
      return true;
    }
    Functions.push_back(flowerFunctionObject(Context, Decl));
    return true;
  }

  bool VisitNamespaceDecl(NamespaceDecl *Decl) {
    if (!Decl || Decl->isImplicit() || Decl->isAnonymousNamespace() ||
        !rangeIsWrittenInMainFile(Context, Decl->getSourceRange())) {
      return true;
    }
    Scopes.push_back(flowerScopeObject(Context, "namespace", Decl->getQualifiedNameAsString(),
                                       Decl, Decl->getSourceRange()));
    return true;
  }

  bool VisitCXXRecordDecl(CXXRecordDecl *Decl) {
    if (!Decl || !Decl->isThisDeclarationADefinition() || Decl->isImplicit() || Decl->isUnion() ||
        !Decl->getIdentifier() || Decl->isLocalClass() || isExternCContext(Decl) ||
        isa<ClassTemplateSpecializationDecl>(Decl) ||
        !rangeIsWrittenInMainFile(Context, Decl->getSourceRange())) {
      return true;
    }
    Scopes.push_back(flowerScopeObject(Context, Decl->isStruct() ? "struct" : "class",
                                       Decl->getQualifiedNameAsString(), Decl,
                                       Decl->getSourceRange()));
    return true;
  }

private:
  ASTContext &Context;
  llvm::json::Array &Functions;
  llvm::json::Array &Scopes;
};

class FlowerConsumer : public ASTConsumer {
public:
  FlowerConsumer(ASTContext &Context, llvm::json::Array &Functions, llvm::json::Array &Scopes)
      : Visitor(Context, Functions, Scopes), Scopes(Scopes) {}

  void HandleTranslationUnit(ASTContext &Context) override {
    const SourceManager &SM = Context.getSourceManager();
    bool Invalid = false;
    llvm::StringRef Buffer = SM.getBufferData(SM.getMainFileID(), &Invalid);
    if (!Invalid) {
      llvm::json::Object Global;
      Global["kind"] = "global";
      Global["name"] = "";
      Global["range"] = llvm::json::Object{{"valid", true},
                                            {"start", int64_t(0)},
                                            {"end", static_cast<int64_t>(Buffer.size())}};
      Global["insert_offset"] = static_cast<int64_t>(Buffer.size());
      llvm::json::Array Offsets;
      for (int64_t Offset : scopeInsertOffsets(Context, Context.getTranslationUnitDecl(), 0,
                                               static_cast<int64_t>(Buffer.size()),
                                               static_cast<int64_t>(Buffer.size()))) {
        Offsets.push_back(Offset);
      }
      Global["insert_offsets"] = std::move(Offsets);
      Scopes.push_back(std::move(Global));
    }
    Visitor.TraverseDecl(Context.getTranslationUnitDecl());
  }

private:
  FlowerVisitor Visitor;
  llvm::json::Array &Scopes;
};

class FlowerAction : public ASTFrontendAction {
public:
  FlowerAction(llvm::json::Array &Functions, llvm::json::Array &Scopes)
      : Functions(Functions), Scopes(Scopes) {}

  std::unique_ptr<ASTConsumer> CreateASTConsumer(CompilerInstance &Compiler,
                                                 llvm::StringRef) override {
    return std::make_unique<FlowerConsumer>(Compiler.getASTContext(), Functions, Scopes);
  }

private:
  llvm::json::Array &Functions;
  llvm::json::Array &Scopes;
};

class FlowerActionFactory : public FrontendActionFactory {
public:
  FlowerActionFactory(llvm::json::Array &Functions, llvm::json::Array &Scopes)
      : Functions(Functions), Scopes(Scopes) {}

  std::unique_ptr<FrontendAction> create() override {
    return std::make_unique<FlowerAction>(Functions, Scopes);
  }

private:
  llvm::json::Array &Functions;
  llvm::json::Array &Scopes;
};

class JsonVisitor : public RecursiveASTVisitor<JsonVisitor> {
public:
  explicit JsonVisitor(ASTContext &Context, llvm::json::Array &Functions)
      : Context(Context), Functions(Functions) {}

  bool TraverseDecl(Decl *Node) {
    if (auto *Function = dyn_cast_or_null<FunctionDecl>(Node)) {
      if (Function->hasBody() && matchesFunction(Function)) {
        emitFunction(Function);
      }
      return true;
    }
    return RecursiveASTVisitor<JsonVisitor>::TraverseDecl(Node);
  }

private:
  void emitFunction(FunctionDecl *Decl) {
    Stmt *Body = Decl->getBody();
    llvm::json::Object Func;
    Func["qualified_name"] = qualifiedName(Decl);
    Func["simple_name"] = Decl->getNameAsString();
    Func["signature"] = rangeObject(Context, Decl->getBeginLoc(), Decl->getLocation());
    Func["body"] = rangeObject(Context, Body->getBeginLoc(), Body->getEndLoc());
    Func["is_constructor_or_destructor"] = isa<CXXConstructorDecl>(Decl) || isa<CXXDestructorDecl>(Decl);
    Func["is_constexpr"] = Decl->isConstexpr();
    Func["is_consteval"] = Decl->isConsteval();

    llvm::json::Array Diagnostics;
    std::vector<StatementInfo> Statements = collectTopLevelStatements(Context, Body);
    if (hasUnsupportedTopLevelStructure(Statements)) {
      Diagnostics.push_back("unsupported goto/label/try structure");
    }

    llvm::json::Array StatementJson;
    std::vector<ControlFrame> ControlStack;
    int NextControlId = 1;
    for (const StatementInfo &Statement : Statements) {
      StatementJson.push_back(statementInfoObject(Context, Statement, ControlStack, NextControlId));
    }
    Func["statements"] = std::move(StatementJson);
    Func["locals"] = collectLocals(Context, Statements);
    Func["block_plan"] = blockPlanObject(Context, Body, 0, ControlStack, -1, NextControlId);
    Func["diagnostics"] = std::move(Diagnostics);
    Functions.push_back(std::move(Func));
  }

  ASTContext &Context;
  llvm::json::Array &Functions;
};

class JsonConsumer : public ASTConsumer {
public:
  explicit JsonConsumer(ASTContext &Context, llvm::json::Array &Functions)
      : Visitor(Context, Functions) {}

  void HandleTranslationUnit(ASTContext &Context) override {
    Visitor.TraverseDecl(Context.getTranslationUnitDecl());
  }

private:
  JsonVisitor Visitor;
};

class JsonAction : public ASTFrontendAction {
public:
  explicit JsonAction(llvm::json::Array &Functions) : Functions(Functions) {}

  std::unique_ptr<ASTConsumer> CreateASTConsumer(CompilerInstance &Compiler,
                                                 llvm::StringRef) override {
    return std::make_unique<JsonConsumer>(Compiler.getASTContext(), Functions);
  }

private:
  llvm::json::Array &Functions;
};

class JsonActionFactory : public FrontendActionFactory {
public:
  explicit JsonActionFactory(llvm::json::Array &Functions) : Functions(Functions) {}

  std::unique_ptr<FrontendAction> create() override {
    return std::make_unique<JsonAction>(Functions);
  }

private:
  llvm::json::Array &Functions;
};

int main(int argc, const char **argv) {
  auto Parser = CommonOptionsParser::create(argc, argv, Category);
  if (!Parser) {
    llvm::errs() << Parser.takeError();
    return 1;
  }

  llvm::json::Array Functions;
  ClangTool Tool(Parser->getCompilations(), Parser->getSourcePathList());
  if (FlowerPlan) {
    llvm::json::Array FlowerFunctions;
    llvm::json::Array FlowerScopes;
    FlowerActionFactory Factory(FlowerFunctions, FlowerScopes);
    int Result = Tool.run(&Factory);
    if (Result != 0) {
      return Result;
    }
    llvm::json::Object Flower;
    Flower["functions"] = std::move(FlowerFunctions);
    Flower["scopes"] = std::move(FlowerScopes);
    llvm::json::Object Root;
    Root["version"] = 1;
    Root["flower_plan"] = std::move(Flower);
    llvm::outs() << llvm::formatv("{0:2}\n", llvm::json::Value(std::move(Root)));
    return 0;
  }

  JsonActionFactory Factory(Functions);
  int Result = Tool.run(&Factory);

  llvm::json::Object Root;
  Root["version"] = 4;
  Root["functions"] = std::move(Functions);
  llvm::outs() << llvm::formatv("{0:2}\n", llvm::json::Value(std::move(Root)));
  return Result;
}
