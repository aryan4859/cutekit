import os
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import TextIO, Union

from . import shell, rules, model, ninja, const, cli

_logger = logging.getLogger(__name__)


def aggregateCincs(target: model.Target, registry: model.Registry) -> set[str]:
    res = set()

    for c in registry.iterEnabled(target):
        if "cpp-root-include" in c.props:
            res.add(c.dirname())
        elif c.type == model.Kind.LIB:
            res.add(str(Path(c.dirname()).parent))

    return set(map(lambda i: f"-I{i}", res))


def aggregateCdefs(target: model.Target) -> set[str]:
    res = set()

    def sanatize(s: str) -> str:
        return s.lower().replace(" ", "_").replace("-", "_").replace(".", "_")

    for k, v in target.props.items():
        if isinstance(v, bool):
            if v:
                res.add(f"-D__ck_{sanatize(k)}__")
        else:
            res.add(f"-D__ck_{sanatize(k)}_{sanatize(str(v))}__")
            res.add(f"-D__ck_{sanatize(k)}_value={str(v)}")

    return res


def buildpath(target: model.Target, component: model.Component, path) -> Path:
    return Path(target.builddir) / component.id / path


# --- Compilation ------------------------------------------------------------ #


def wilcard(component: model.Component, wildcards: list[str]) -> list[str]:
    dirs = [component.dirname()] + list(
        map(lambda d: os.path.join(component.dirname(), d), component.subdirs)
    )
    return shell.find(dirs, list(wildcards), recusive=False)


def compile(
    w: ninja.Writer,
    target: model.Target,
    component: model.Component,
    rule: str,
    srcs: list[str],
) -> list[str]:
    res: list[str] = []
    for src in srcs:
        rel = Path(src).relative_to(component.dirname())
        dest = buildpath(target, component, "obj") / rel.with_suffix(".o")
        t = target.tools[rule]
        w.build(str(dest), rule, inputs=src, order_only=t.files)
        res.append(str(dest))
    return res


# --- Ressources ------------------------------------------------------------- #


def listRes(component: model.Component) -> list[str]:
    return shell.find(str(component.subpath("res")))


def compileRes(
    w: ninja.Writer,
    target: model.Target,
    component: model.Component,
) -> list[str]:
    res: list[str] = []
    for r in listRes(component):
        rel = Path(r).relative_to(component.subpath("res"))
        dest = buildpath(target, component, "res") / rel
        w.build(str(dest), "cp", r)
        res.append(str(dest))
    return res


# --- Linking ---------------------------------------------------------------- #


def outfile(target: model.Target, component: model.Component) -> str:
    if component.type == model.Kind.LIB:
        return str(buildpath(target, component, f"lib/{component.id}.a"))
    else:
        return str(buildpath(target, component, f"bin/{component.id}.out"))


def collectLibs(
    registry: model.Registry, target: model.Target, component: model.Component
) -> list[str]:
    res: list[str] = []
    for r in component.resolved[target.id].resolved:
        req = registry.lookup(r, model.Component)
        assert req is not None  # model.Resolver has already checked this

        if r == component.id:
            continue
        if not req.type == model.Kind.LIB:
            raise RuntimeError(f"Component {r} is not a library")
        res.append(outfile(target, req))
    return res


def link(
    w: ninja.Writer,
    registry: model.Registry,
    target: model.Target,
    component: model.Component,
) -> str:
    w.newline()
    out = outfile(target, component)

    objs = []
    objs += compile(w, target, component, "cc", wilcard(component, ["*.c"]))
    objs += compile(
        w, target, component, "cxx", wilcard(component, ["*.cpp", "*.cc", "*.cxx"])
    )
    objs += compile(
        w, target, component, "as", wilcard(component, ["*.s", "*.asm", "*.S"])
    )

    res = compileRes(w, target, component)
    libs = collectLibs(registry, target, component)
    if component.type == model.Kind.LIB:
        w.build(out, "ar", objs, implicit=res)
    else:
        w.build(out, "ld", objs + libs, implicit=res)
    return out


# --- Phony ------------------------------------------------------------------ #


def all(w: ninja.Writer, registry: model.Registry, target: model.Target) -> list[str]:
    all: list[str] = []
    for c in registry.iterEnabled(target):
        all.append(link(w, registry, target, c))
    w.build("all", "phony", all)
    w.default("all")
    return all


def gen(out: TextIO, target: model.Target, registry: model.Registry):
    w = ninja.Writer(out)

    w.comment("File generated by the build system, do not edit")
    w.newline()

    w.variable("builddir", target.builddir)
    w.variable("hashid", target.hashid)

    w.separator("Tools")

    w.variable("cincs", " ".join(aggregateCincs(target, registry)))
    w.variable("cdefs", " ".join(aggregateCdefs(target)))
    w.newline()

    w.rule("cp", "cp $in $out")
    for i in target.tools:
        tool = target.tools[i]
        rule = rules.rules[i]
        w.variable(i, tool.cmd)
        w.variable(i + "flags", " ".join(rule.args + tool.args))
        w.rule(
            i,
            f"{tool.cmd} {rule.rule.replace('$flags',f'${i}flags')}",
            depfile=rule.deps,
        )
        w.newline()

    w.separator("Build")

    all(w, registry, target)


@dataclass
class Product:
    path: Path
    target: model.Target
    component: model.Component


def build(
    target: model.Target,
    registry: model.Registry,
    components: Union[list[model.Component], model.Component, None] = None,
) -> list[Product]:
    all = False
    shell.mkdir(target.builddir)
    ninjaPath = os.path.join(target.builddir, "build.ninja")
    with open(ninjaPath, "w") as f:
        gen(f, target, registry)

    if components is None:
        all = True
        components = list(registry.iterEnabled(target))

    if isinstance(components, model.Component):
        components = [components]

    products: list[Product] = []
    for c in components:
        products.append(
            Product(
                path=Path(outfile(target, c)),
                target=target,
                component=c,
            )
        )

    outs = list(map(lambda p: str(p.path), products))
    if all:
        shell.exec("ninja", "-v", "-f", ninjaPath)
    else:
        shell.exec("ninja", "-v", "-f", ninjaPath, *outs)
    return products


# --- Commands --------------------------------------------------------------- #


@cli.command("b", "build", "Build a component or all components")
def buildCmd(args: cli.Args):
    registry = model.Registry.use(args)
    target = model.Target.use(args)
    componentSpec = args.consumeArg()
    if componentSpec is None:
        raise RuntimeError("No component specified")
    component = registry.lookup(componentSpec, model.Component)
    build(target, registry, component)[0]


@cli.command("r", "run", "Run a component")
def runCmd(args: cli.Args):
    registry = model.Registry.use(args)
    target = model.Target.use(args)
    debug = args.consumeOpt("debug", False) is True

    componentSpec = args.consumeArg()
    if componentSpec is None:
        raise RuntimeError("No component specified")

    component = registry.lookup(componentSpec, model.Component)
    if component is None:
        raise RuntimeError(f"Component {componentSpec} not found")

    product = build(target, registry, component)[0]

    os.environ["CK_TARGET"] = target.id
    os.environ["CK_COMPONENT"] = product.component.id
    os.environ["CK_BUILDDIR"] = target.builddir

    shell.exec(*(["lldb", "-o", "run"] if debug else []), str(product.path), *args.args)


@cli.command("t", "test", "Run all test targets")
def testCmd(args: cli.Args):
    # This is just a wrapper around the `run` command that try
    # to run a special hook component named __tests__.
    args.args.insert(0, "__tests__")
    runCmd(args)


@cli.command("d", "debug", "Debug a component")
def debugCmd(args: cli.Args):
    # This is just a wrapper around the `run` command that
    # always enable debug mode.
    args.opts["debug"] = True
    runCmd(args)


@cli.command("c", "clean", "Clean build files")
def cleanCmd(args: cli.Args):
    model.Project.use(args)
    shell.rmrf(const.BUILD_DIR)


@cli.command("n", "nuke", "Clean all build files and caches")
def nukeCmd(args: cli.Args):
    model.Project.use(args)
    shell.rmrf(const.PROJECT_CK_DIR)
