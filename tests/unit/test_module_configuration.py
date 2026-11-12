import os
from pathlib import Path

from jolink_runtime.launch.compiler_profile import classify_compiler_arguments
from jolink_runtime.launch.module_configuration import module_configuration, unrepresented_patches, descriptor_inputs


def test_explicit_module_flags_are_retained_and_resolved(tmp_path):
    args = ['--module-path', os.pathsep.join(('lib-one', 'lib two')),
            '--patch-module', 'example.app=' + os.pathsep.join(('target/classes', 'src/test/java')),
            '--add-reads=example.app=ALL-UNNAMED', '--add-exports', 'example.app/internal=other']
    profile = classify_compiler_arguments(args)
    assert not profile.unresolved_arguments
    facts = module_configuration({'moduleName':None, 'modulePath':['native-path']}, profile, basedir=tmp_path)
    assert facts['module_path'] == [str(tmp_path/'lib-one'), str(tmp_path/'lib two')]
    assert facts['patch_module'] == 'example.app'
    assert facts['patch_paths'] == [str(tmp_path/'target/classes'), str(tmp_path/'src/test/java')]
    assert facts['add_reads'] == ['example.app=ALL-UNNAMED']
    assert facts['add_exports'] == ['example.app/internal=other']
    assert not unrepresented_patches(profile, (tmp_path/'target/classes',tmp_path/'src/test/java'),tmp_path)
    assert unrepresented_patches(profile, (tmp_path/'src/test/java',),tmp_path) == profile.patch_modules


def test_multiple_patches_do_not_silently_drop_to_last_one():
    args = ['--patch-module','first=one','--patch-module=second=two']
    profile = classify_compiler_arguments(args)
    assert profile.patch_modules == ('first=one','second=two')
    assert profile.unresolved_arguments == tuple(args)


def test_native_facts_and_empty_modulepath_override(tmp_path):
    native={'moduleName':'example.app', 'moduleDescriptor':str(tmp_path/'src/module-info.java'),
            'modulePath':[str(tmp_path/'lib')], 'patchModule':'example.main',
            'addReads':['example.main=ALL-UNNAMED']}
    baseline=module_configuration(native,classify_compiler_arguments([]),basedir=tmp_path)
    assert baseline['module_name']=='example.app' and baseline['patch_module']=='example.main'
    assert baseline['module_path']==[str(tmp_path/'lib')]
    empty=module_configuration(native,classify_compiler_arguments(['--module-path=']),basedir=tmp_path)
    assert empty['module_path']==[]


def test_descriptor_inputs_include_missing_main_and_test_sources(tmp_path):
    roots=[str(tmp_path/'main'),str(tmp_path/'test')]
    assert descriptor_inputs(({'source_roots':roots[:1], 'test_source_roots':roots[1:]},)) == (
        tmp_path/'main/module-info.java',tmp_path/'test/module-info.java')
